import datetime

from sqlalchemy import select

from meshtastic.portnums_pb2 import PortNum
from meshtastic.mesh_pb2 import User, HardwareModel
from meshview import database
from meshview import decode_payload
from meshview.models import Packet, PacketSeen, Node
from meshview import notify


async def process_envelope(topic, env):
    if not env.packet.id:
        return

    async with database.async_session() as session:
        result = await session.execute(select(Packet).where(Packet.id == env.packet.id))
        new_packet = False
        packet = result.scalar_one_or_none()
        if not packet:
            new_packet = True
            packet = Packet(
                id=env.packet.id,
                portnum=env.packet.decoded.portnum,
                from_node_id=getattr(env.packet, "from"),
                to_node_id=env.packet.to,
                payload=env.packet.SerializeToString(),
                import_time=datetime.datetime.utcnow(),
            )
            session.add(packet)

        result = await session.execute(
            select(PacketSeen).where(
                PacketSeen.packet_id == env.packet.id,
                PacketSeen.node_id == int(env.gateway_id[1:], 16),
                PacketSeen.rx_time == env.packet.rx_time,
            )
        )
        seen = None
        if not result.scalar_one_or_none():
            seen = PacketSeen(
                packet_id=env.packet.id,
                node_id=int(env.gateway_id[1:], 16),
                channel=env.channel_id,
                rx_time=env.packet.rx_time,
                rx_snr=env.packet.rx_snr,
                rx_rssi=env.packet.rx_rssi,
                hop_limit=env.packet.hop_limit,
                topic=topic,
                import_time=datetime.datetime.utcnow(),
            )
            session.add(seen)

        if env.packet.decoded.portnum == PortNum.NODEINFO_APP:
            user = decode_payload.decode_payload(
                PortNum.NODEINFO_APP, env.packet.decoded.payload
            )
            if user:
                result = await session.execute(select(Node).where(Node.id == user.id))
                if user.id and user.id[0] == "!":
                    try:
                        node_id = int(user.id[1:], 16)
                    except ValueError:
                        node_id = None
                        pass
                else:
                    node_id = None

                try:
                    hw_model = HardwareModel.Name(user.hw_model)
                except ValueError:
                    hw_model = "unknown"

                if node := result.scalar_one_or_none():
                    node.node_id = node_id
                    node.long_name = user.long_name
                    node.short_name = user.short_name
                    node.hw_model = hw_model
                else:
                    node = Node(
                        id=user.id,
                        node_id=node_id,
                        long_name=user.long_name,
                        short_name=user.short_name,
                        hw_model=hw_model,
                    )
                session.add(node)

        await session.commit()
        if new_packet:
            await packet.awaitable_attrs.to_node
            await packet.awaitable_attrs.from_node
            notify.notify_packet(packet.to_node_id, packet)
            notify.notify_packet(packet.from_node_id, packet)
            notify.notify_packet(None, packet)
        if seen:
            notify.notify_uplinked(seen.node_id, packet)


async def get_node(node_id):
    async with database.async_session() as session:
        result = await session.execute(select(Node).where(Node.node_id == node_id))
        return result.scalar_one_or_none()


async def get_fuzzy_nodes(query):
    async with database.async_session() as session:
        q = select(Node).where(
            Node.id.ilike(query + "%")
            | Node.long_name.ilike(query + "%")
            | Node.short_name.ilike(query + "%")
        )
        result = await session.execute(q)
        return result.scalars()


async def get_packets(node_id=None, portnum=None):
    async with database.async_session() as session:
        q = select(Packet)

        if node_id:
            q = q.where(
                (Packet.from_node_id == node_id) | (Packet.to_node_id == node_id)
            )
        if portnum:
            q = q.where(Packet.portnum == portnum)
        result = await session.execute(q.limit(500).order_by(Packet.import_time.desc()))
        return result.scalars()


async def get_packet(packet_id):
    async with database.async_session() as session:
        q = select(Packet).where(Packet.id == packet_id)
        result = await session.execute(q)
        return result.scalar_one_or_none()


async def get_uplinked_packets(node_id):
    async with database.async_session() as session:
        result = await session.execute(
            select(Packet)
            .join(PacketSeen)
            .where(PacketSeen.node_id == node_id)
            .order_by(Packet.import_time.desc())
            .limit(500)
        )
        return result.scalars()


async def get_packets_seen(packet_id):
    async with database.async_session() as session:
        result = await session.execute(
            select(PacketSeen)
            .where(PacketSeen.packet_id == packet_id)
            .order_by(PacketSeen.import_time.desc())
        )
        return result.scalars()