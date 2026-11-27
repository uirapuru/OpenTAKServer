"""cot_parser podpisuje nadawcę czatu przy rozsyłaniu, na każdej drodze.

Do cot_parser trafiają nie tylko połączenia ATAK (EudHandler), ale też np.
zdarzenia z paczki misji (PUT .../contents/missionpackage), które EudHandler
omijają. Podpis klienta znika przed zapisem do bazy, a serwerowy powstaje
dopiero w route_cot. Zapisane i odtwarzane z misji zdarzenia nie mają więc
podpisu i nie da się ich użyć ponownie jako podpisanego polecenia.
"""

from bs4 import BeautifulSoup

from tests.test_cot_parser_origin import (  # noqa: F401 (fikstury)
    controller,
    deliver,
    make_event,
    parse,
    published,
    SENDER_UID,
    sender_eud,
    user_id,
)

FORGED = '<__taklab_sender username="admin"/>'


def senders(cot):
    return [t["username"] for t in BeautifulSoup(cot, "xml").find_all("__taklab_sender")]


def test_forged_sender_on_dm_replaced_by_account_of_publisher(controller, user_id, sender_eud):
    deliver(controller, make_event(FORGED + '<marti><dest callsign="stacja1"/></marti>'), user_id)

    [(routing_key, body)] = published(controller, "dms")
    assert routing_key == "stacja1"
    assert senders(body["cot"]) == ["kaszub"]


def test_forged_sender_on_broadcast_chat_removed_without_stamp(controller, user_id, sender_eud):
    # Stacja przyjmuje tylko wiadomości bezpośrednie; rozgłoszenie idzie bez podpisu
    deliver(controller, make_event(FORGED + "<__chat/>"), user_id)

    bodies = published(controller, "groups")
    assert bodies
    for _, body in bodies:
        assert senders(body["cot"]) == []


def test_chat_without_account_goes_out_unsigned(controller, sender_eud):
    deliver(controller, make_event(FORGED + '<marti><dest callsign="stacja1"/></marti>'), None)

    [(_, body)] = published(controller, "dms")
    assert senders(body["cot"]) == []


def test_stored_chat_has_no_sender_stamp(app, controller, user_id, sender_eud):
    from opentakserver.extensions import db
    from opentakserver.models.CoT import CoT

    deliver(controller, make_event(FORGED + '<marti><dest callsign="stacja1"/></marti>'), user_id)

    with app.app_context():
        [stored] = db.session.execute(db.select(CoT)).scalars()
        assert senders(stored.xml) == []


def test_route_cot_called_directly_stamps(controller, user_id):
    event = parse(make_event(FORGED + '<marti><dest uid="station-uid"/></marti>'))

    controller.route_cot(event, SENDER_UID, user_id)

    [(_, body)] = published(controller, "dms")
    assert senders(body["cot"]) == ["kaszub"]
