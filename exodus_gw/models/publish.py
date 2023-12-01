import os
import uuid
from datetime import datetime
from typing import Optional, Union

from fastapi import HTTPException
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    event,
    func,
    inspect,
)
from sqlalchemy.orm import Bundle, Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from .base import Base

# The resolution of links SHOULD be isolated to within the current publish;
# this is very important. However, the original implementation of the code
# did not do that and allowed links to be resolved across any publish,
# see RHELDST-21893.
#
# That is being fixed, but the problem is that clients may have come to
# rely on it. In particular, if pub/pulp/exodus integration is *disabled*,
# we rely on it.
#
# Thus, this semi-hidden setting acts as an escape hatch to re-enable
# the old buggy behavior if it turns out to be needed. Not a proper setting.
#
# Only set this if you really know it's needed! With any luck, this branch
# should be removed quickly.
LINK_ISOLATION = (os.environ.get("EXODUS_GW_LINK_ISOLATION") or "1") == "1"


class Publish(Base):
    __tablename__ = "publishes"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    env: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    updated: Mapped[Optional[datetime]] = mapped_column(DateTime())
    items = relationship(
        "Item", back_populates="publish", cascade="all, delete-orphan"
    )

    def resolve_links(self):
        db = inspect(self).session
        # Store only publish items with link targets.
        ln_items = (
            db.query(Item)
            .with_for_update()
            .filter(Item.publish_id == self.id)
            .filter(
                func.coalesce(Item.link_to, "") != ""  # pylint: disable=E1102
            )
            .all()
        )
        # Collect link targets of linked items for finding matches.
        ln_item_paths = [item.link_to for item in ln_items]

        # Store only necessary fields from matching items to conserve memory.
        match = Bundle(
            "match", Item.web_uri, Item.object_key, Item.content_type
        )
        query = db.query(match).filter(Item.web_uri.in_(ln_item_paths))
        if LINK_ISOLATION:
            # See comments above where LINK_ISOLATION is set.
            # This path should become the only path ASAP!
            query = query.filter(Item.publish_id == self.id)

        matches = {
            row.match.web_uri: {
                "object_key": row.match.object_key,
                "content_type": row.match.content_type,
            }
            for row in query
        }

        for ln_item in ln_items:
            match = matches.get(ln_item.link_to)

            if (
                not match
                or not match.get("object_key")
                or not match.get("content_type")
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Unable to resolve item object_key:"
                        "\n\tURI: '%s'\n\tLink: '%s'"
                    )
                    % (ln_item.web_uri, ln_item.link_to),
                )

            ln_item.object_key = match.get("object_key")
            ln_item.content_type = match.get("content_type")


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint(
            "publish_id", "web_uri", name="items_publish_id_web_uri_key"
        ),
    )

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    web_uri: Mapped[str] = mapped_column(String)
    object_key: Mapped[Optional[str]] = mapped_column(String)
    content_type: Mapped[Optional[str]] = mapped_column(String)
    link_to: Mapped[Optional[str]] = mapped_column(String)

    dirty: Mapped[bool] = mapped_column(Boolean, default=True)
    """True if item still needs to be written to DynamoDB."""

    updated: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )
    """Last modification/creation time of the item.

    This will be eventually persisted as `from_date` on the corresponding
    DynamoDB item.
    """

    publish_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("publishes.id")
    )

    publish = relationship("Publish", back_populates="items")


@event.listens_for(Publish, "before_update")
@event.listens_for(Item, "before_update")
def set_updated(_mapper, _connection, entity: Union[Publish, Item]):
    entity.updated = datetime.utcnow()
