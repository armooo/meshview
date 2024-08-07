import datetime

from sqlalchemy import select, func
from sqlalchemy.orm import lazyload

from meshtastic.config_pb2 import Config
from meshtastic.portnums_pb2 import PortNum
from meshtastic.mesh_pb2 import User, HardwareModel
from meshview import database
from meshview import decode_payload
from meshview.models import Packet, PacketSeen, Node, Traceroute
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
                hop_start=env.packet.hop_start,
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
                try:
                   role = Config.DeviceConfig.Role.Name(user.role)
                except ValueError:
                    role = "unknown"

                if node := result.scalar_one_or_none():
                    node.node_id = node_id
                    node.long_name = user.long_name
                    node.short_name = user.short_name
                    node.hw_model = hw_model
                    node.role = role
                else:
                    node = Node(
                        id=user.id,
                        node_id=node_id,
                        long_name=user.long_name,
                        short_name=user.short_name,
                        hw_model=hw_model,
                        role=role,
                    )
                    session.add(node)

        if env.packet.decoded.portnum == PortNum.POSITION_APP:
            position = decode_payload.decode_payload(
                PortNum.POSITION_APP, env.packet.decoded.payload
            )
            if position and position.latitude_i and position.longitude_i:
                from_node_id = getattr(env.packet, 'from')
                node = (await session.execute(select(Node).where(Node.node_id == from_node_id))).scalar_one_or_none()
                if node:
                    node.last_lat = position.latitude_i
                    node.last_long = position.longitude_i
                    session.add(node)

        if env.packet.decoded.portnum == PortNum.TRACEROUTE_APP:
            packet_id = None
            if env.packet.decoded.want_response:
                packet_id = env.packet.id
            else:
                result = await session.execute(select(Packet).where(Packet.id == env.packet.decoded.request_id))
                if result.scalar_one_or_none():
                    packet_id = env.packet.decoded.request_id
            if packet_id is not None:
                session.add(Traceroute(
                    packet_id=packet_id,
                    route=env.packet.decoded.payload,
                    done=not env.packet.decoded.want_response,
                    gateway_node_id=int(env.gateway_id[1:], 16),
                    import_time=datetime.datetime.utcnow(),
                ))

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


async def get_packets(node_id=None, portnum=None, since=None, limit=500, before=None, after=None):
    async with database.async_session() as session:
        q = select(Packet)

        if node_id:
            q = q.where(
                (Packet.from_node_id == node_id) | (Packet.to_node_id == node_id)
            )
        if portnum:
            q = q.where(Packet.portnum == portnum)
        if since:
            q = q.where(Packet.import_time > (datetime.datetime.utcnow() - since))
        if before:
            q = q.where(Packet.import_time < before)
        if after:
            q = q.where(Packet.import_time > after)
        if limit is not None:
            q = q.limit(limit)

        result = await session.execute(q.order_by(Packet.import_time.desc()))
        return result.scalars()


async def get_packets_from(node_id=None, portnum=None, since=None, limit=500):
    async with database.async_session() as session:
        q = select(Packet)

        if node_id:
            q = q.where(
                Packet.from_node_id == node_id
            )
        if portnum:
            q = q.where(Packet.portnum == portnum)
        if since:
            q = q.where(Packet.import_time > (datetime.datetime.utcnow() - since))
        result = await session.execute(q.limit(limit).order_by(Packet.import_time.desc()))
        return result.scalars()


async def get_packet(packet_id):
    async with database.async_session() as session:
        q = select(Packet).where(Packet.id == packet_id)
        result = await session.execute(q)
        return result.scalar_one_or_none()


async def get_uplinked_packets(node_id, portnum=None):
    async with database.async_session() as session:
        q = select(Packet).join(PacketSeen).where(PacketSeen.node_id == node_id).order_by(Packet.import_time.desc()).limit(500)
        if portnum:
            q = q.where(Packet.portnum == portnum)
        result = await session.execute(q)
        return result.scalars()


async def get_packets_seen(packet_id):
    async with database.async_session() as session:
        result = await session.execute(
            select(PacketSeen)
            .where(PacketSeen.packet_id == packet_id)
            .order_by(PacketSeen.import_time.desc())
        )
        return result.scalars()


async def has_packets(node_id, portnum):
    async with database.async_session() as session:
        return bool(
            (await session.execute(
                    select(Packet.id).where(Packet.from_node_id == node_id).limit(1)
            )).scalar()
        )


async def get_traceroute(packet_id):
    async with database.async_session() as session:
        result = await session.execute(
                select(Traceroute)
                .where(Traceroute.packet_id == packet_id)
                .order_by(Traceroute.import_time)
        )
        return result.scalars()


async def get_traceroutes(since):
    async with database.async_session() as session:
        result = await session.execute(
                select(Traceroute)
                .join(Packet)
                .where(Traceroute.import_time > (datetime.datetime.utcnow() - since))
                .order_by(Traceroute.import_time)
        )
        return result.scalars()


async def get_mqtt_neighbors(since):
    async with database.async_session() as session:
        result = await session.execute(select(PacketSeen, Packet)
            .join(Packet)
            .where(
                (PacketSeen.hop_limit == PacketSeen.hop_start)
                & (PacketSeen.hop_start != 0)
                & (PacketSeen.import_time > (datetime.datetime.utcnow() - since))
            )
            .options(
                lazyload(Packet.from_node),
                lazyload(Packet.to_node),
            )
        )
        return result

