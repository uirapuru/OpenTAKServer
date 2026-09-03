import datetime
import enum
from dataclasses import dataclass

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from opentakserver.extensions import db
from opentakserver.functions import iso8601_string_from_datetime
from opentakserver.models.MissionRole import MissionRole


class InvitationTypeEnum(str, enum.Enum):
    clientUid = "clientUid"
    callsign = "callsign"
    userName = "userName"
    group = "group"
    team = "team"


@dataclass
class MissionInvitation(db.Model):
    __tablename__ = "mission_invitations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mission_name: Mapped[str] = mapped_column(String(255), ForeignKey("missions.name"), nullable=True)
    mission_guid: Mapped[str] = mapped_column(String(255), nullable=True)
    client_uid: Mapped[str] = mapped_column(
        String(255), ForeignKey("euds.uid", ondelete="CASCADE"), nullable=True
    )
    callsign: Mapped[str] = mapped_column(
        String(255), ForeignKey("euds.callsign", ondelete="CASCADE"), nullable=True
    )
    username: Mapped[str] = mapped_column(String(255), ForeignKey("user.username"), nullable=True)
    group_name: Mapped[str] = mapped_column(String(255), nullable=True)
    team_name: Mapped[str] = mapped_column(String(255), ForeignKey("teams.name"), nullable=True)
    creator_uid: Mapped[str] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(255), nullable=True)
    type: Mapped[str] = mapped_column(String(255), nullable=True, default="callsign")

    eud_uid = relationship("EUD", foreign_keys=[client_uid], uselist=False)
    eud_callsign = relationship("EUD", foreign_keys=[callsign], uselist=False)
    user = relationship("User", back_populates="mission_invitations", uselist=False)
    team = relationship("Team", back_populates="mission_invitations", uselist=False)
    mission = relationship("Mission", back_populates="invitations", uselist=False)

    def serialize(self):
        return {
            "mission_name": self.mission_name,
            "mission_guid": self.mission_guid,
            "client_uid": self.client_uid,
            "callsign": self.callsign,
            "username": self.username,
            "group_name": self.group_name,
            "team_name": self.team_name,
            "creator_uid": self.creator_uid,
            "role": self.role,
            "type": self.type,
        }

    def to_json(self):
        return self.serialize()

    def invitee(self) -> str | None:
        """Name the invited party the way its invitation type says.

        TAK Server identifies the invitee by a different value for each type:
        a device by its UID, a person by username, a team or a group by name.
        The type column decides which of these columns carries it.
        """
        by_type = {
            InvitationTypeEnum.clientUid: self.client_uid,
            InvitationTypeEnum.callsign: self.callsign,
            InvitationTypeEnum.userName: self.username,
            InvitationTypeEnum.group: self.group_name,
            InvitationTypeEnum.team: self.team_name,
        }

        return by_type.get(self.type) or (
            self.client_uid or self.callsign or self.username
            or self.team_name or self.group_name
        )

    def role_json(self) -> dict:
        """Spell out the role this invitation was written with.

        The row records which role it grants, so reporting every invitation as
        a subscriber understated what the owner of a mission may do with it.

        The permission list is copied, not handed out: the source dicts live on
        the MissionRole class and a caller editing one would change the role for
        every invitation the server ever answers with.

        Both name and type carry the role name. TAK Server sends type, CloudTAK
        reads name and drops every key its schema does not know - given only
        type it shows the role as "Unknown Role".
        """
        by_role = {
            MissionRole.MISSION_OWNER: MissionRole.OWNER_ROLE,
            MissionRole.MISSION_READ_ONLY: MissionRole.READ_ONLY_ROLE,
            MissionRole.MISSION_SUBSCRIBER: MissionRole.SUBSCRIBER_ROLE,
        }
        role = by_role.get(self.role, MissionRole.SUBSCRIBER_ROLE)

        return {
            "name": role["type"],
            "type": role["type"],
            "permissions": list(role["permissions"]),
        }

    def to_marti_json(self):
        json = {
            "missionName": self.mission_name,
            # A role is a single object, not a list of role names. Clients
            # read its permissions to decide what the invited party may do;
            # a list makes the invitation unreadable to them.
            "role": self.role_json(),
            "type": self.type,
            "creatorUid": self.creator_uid,
            "createTime": iso8601_string_from_datetime(),
            "token": "",
            "missionGuid": self.mission.guid or self.mission_guid,
        }

        # Left out entirely when unknown: clients type this field as a string,
        # so a null is worse for them than an absent key.
        invitee = self.invitee()
        if invitee:
            json["invitee"] = invitee

        return json
