"""Shape of a mission invitation as clients read it.

CloudTAK refused to sync Data Sync missions with a 400 naming two fields of
the very first invitation: invitee had to be a string and role had to be an
object. Both came straight out of to_marti_json().
"""

import pytest

from opentakserver.models.Mission import Mission
from opentakserver.models.MissionInvitation import MissionInvitation
from opentakserver.models.MissionRole import MissionRole


@pytest.fixture(autouse=True)
def mappers(app):
    """Every model has to be imported before SQLAlchemy can map any of them,
    and building the application is what imports them all."""
    return app


def build(**attributes):
    invitation = MissionInvitation()
    invitation.mission = Mission()
    invitation.mission.guid = "0a3b1f5c-2f37-4d67-9a1e-9ec6cf6d3f01"
    invitation.mission_name = "OSTROGA"
    invitation.mission_guid = invitation.mission.guid
    invitation.creator_uid = "ANDROID-c0ffee"
    invitation.type = "clientUid"
    for name, value in attributes.items():
        setattr(invitation, name, value)
    return invitation


def test_invitee_is_a_string_not_a_related_row():
    """The field used to carry the EUD relationship object, so every client
    typing this field as a string rejected the whole invitation list."""
    invitation = build(client_uid="ANDROID-1234")

    assert invitation.to_marti_json()["invitee"] == "ANDROID-1234"


def test_role_is_a_single_object():
    """A list of role names tells the client nothing about permissions."""
    role = build(client_uid="ANDROID-1234").to_marti_json()["role"]

    assert isinstance(role, dict)
    assert role["type"] == MissionRole.MISSION_SUBSCRIBER
    assert MissionRole.MISSION_READ in role["permissions"]


def test_invitee_follows_the_invitation_type():
    """TAK Server names the invited party by UID, by username or by team name,
    depending on the type - reading one column for every type would leave the
    other kinds of invitation without an invitee."""
    cases = {
        "clientUid": ("client_uid", "ANDROID-1234"),
        "callsign": ("callsign", "SZPERACZ"),
        "userName": ("username", "kowalski"),
        "team": ("team_name", "Cyan"),
        "group": ("group_name", "__ANON__"),
    }

    for invitation_type, (column, value) in cases.items():
        invitation = build(type=invitation_type, **{column: value})

        assert invitation.to_marti_json()["invitee"] == value, invitation_type


def test_unknown_invitee_leaves_the_field_out():
    """An absent key is valid for clients; a null one is not."""
    invitation = build(type="clientUid")

    assert "invitee" not in invitation.to_marti_json()


def test_serialize_reads_the_group_column_that_exists():
    """serialize() reached for self.group, a column this model never had."""
    invitation = build(client_uid="ANDROID-1234", group_name="__ANON__")

    assert invitation.serialize()["group_name"] == "__ANON__"
