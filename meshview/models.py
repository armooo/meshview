from datetime import datetime

from sqlalchemy.orm import DeclarativeBase, foreign
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import mapped_column, relationship, Mapped
from sqlalchemy import ForeignKey, BigInteger


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Node(Base):
    __tablename__ = "node"
    id: Mapped[str] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(BigInteger, nullable=True, unique=True)
    long_name: Mapped[str]
    short_name: Mapped[str]
    hw_model: Mapped[str]
    last_lat: Mapped[int] = mapped_column(BigInteger, nullable=True)
    last_long: Mapped[int] = mapped_column(BigInteger, nullable=True)


class Packet(Base):
    __tablename__ = "packet"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    portnum: Mapped[int]
    from_node_id: Mapped[int] = mapped_column(BigInteger)
    from_node: Mapped["Node"] = relationship(
        primaryjoin="Packet.from_node_id == foreign(Node.node_id)", lazy="joined"
    )
    to_node_id: Mapped[int] = mapped_column(BigInteger)
    to_node: Mapped["Node"] = relationship(
        primaryjoin="Packet.to_node_id == foreign(Node.node_id)", lazy="joined"
    )
    payload: Mapped[bytes]
    import_time: Mapped[datetime]


class PacketSeen(Base):
    __tablename__ = "packet_seen"
    packet_id = mapped_column(ForeignKey("packet.id"), primary_key=True)
    node_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    node: Mapped["Node"] = relationship(
        lazy="joined", primaryjoin="PacketSeen.node_id == foreign(Node.node_id)"
    )
    rx_time: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hop_limit: Mapped[int]
    channel: Mapped[str]
    rx_snr: Mapped[float] = mapped_column(nullable=True)
    rx_rssi: Mapped[int] = mapped_column(nullable=True)
    topic: Mapped[str]
    import_time: Mapped[datetime]


class Traceroute(Base):
    __tablename__ = "traceroute"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    packet_id = mapped_column(ForeignKey("packet.id"))
    gateway_node_id: Mapped[int] = mapped_column(BigInteger)
    done: Mapped[bool]
    route: Mapped[bytes]
    import_time: Mapped[datetime]

