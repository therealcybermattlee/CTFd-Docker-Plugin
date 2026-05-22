from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from CTFd.models import db
from CTFd.models import Challenges


# Preset resource tiers for container challenges: name -> (vCPU, memory in MB).
# "small" matches the historical default (1 vCPU / 1.5 GB). The largest tier
# is capped to Azure Container Instances' per-container ceiling (4 vCPU / 16 GB).
CONTAINER_SIZES = {
    "small": (1.0, 1536),
    "medium": (2.0, 4096),
    "large": (4.0, 16384),
}
DEFAULT_CONTAINER_SIZE = "small"


def resolve_size(name):
    """Map a size-tier name to (cpu_vcpu, memory_mb), defaulting to small."""
    return CONTAINER_SIZES.get(
        name or DEFAULT_CONTAINER_SIZE, CONTAINER_SIZES[DEFAULT_CONTAINER_SIZE]
    )


class ContainerChallengeModel(Challenges):
    __mapper_args__ = {"polymorphic_identity": "container"}
    id = db.Column(
        db.Integer, db.ForeignKey("challenges.id", ondelete="CASCADE"), primary_key=True
    )
    image = db.Column(db.Text)
    port = db.Column(db.Integer)
    command = db.Column(db.Text, default="")
    volumes = db.Column(db.Text, default="")
    # Resource tier name (key into CONTAINER_SIZES). "small" == legacy default.
    size = db.Column(db.String(16), default=DEFAULT_CONTAINER_SIZE)

    # Dynamic challenge properties
    initial = db.Column(db.Integer, default=0)
    minimum = db.Column(db.Integer, default=0)
    decay = db.Column(db.Integer, default=0)

    def __init__(self, *args, **kwargs):
        super(ContainerChallengeModel, self).__init__(**kwargs)
        self.value = kwargs["initial"]


class ContainerInfoModel(db.Model):
    __mapper_args__ = {"polymorphic_identity": "container_info"}
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    container_id = db.Column(db.String(512), nullable=True, index=True)
    challenge_id = db.Column(
        db.Integer, db.ForeignKey("challenges.id", ondelete="CASCADE")
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE")
    )
    port = db.Column(db.Integer, nullable=True)
    hostname = db.Column(db.String(512), nullable=True)
    status = db.Column(db.String(32), default="provisioning", nullable=False)
    error_message = db.Column(db.Text, nullable=True)
    timestamp = db.Column(db.Integer)
    expires = db.Column(db.Integer)
    user = relationship("Users", foreign_keys=[user_id])
    challenge = relationship(ContainerChallengeModel,
                             foreign_keys=[challenge_id])
    __table_args__ = (
        db.UniqueConstraint("challenge_id", "user_id",
                            name="uq_container_chal_user"),
    )


class ContainerSettingsModel(db.Model):
    __mapper_args__ = {"polymorphic_identity": "container_settings"}
    key = db.Column(db.String(512), primary_key=True)
    value = db.Column(db.Text)
