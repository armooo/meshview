import base64
import asyncio

import aiomqtt
from google.protobuf.message import DecodeError
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from meshtastic.mqtt_pb2 import ServiceEnvelope

KEY = base64.b64decode("1PG7OiApB1nwvP+rz05pAQ==")


def decrypt(packet):
    if packet.HasField("decoded"):
        return
    packet_id = packet.id.to_bytes(8, "little")
    from_node_id = getattr(packet, "from").to_bytes(8, "little")
    nonce = packet_id + from_node_id

    cipher = Cipher(algorithms.AES(KEY), modes.CTR(nonce))
    decryptor = cipher.decryptor()
    raw_proto = decryptor.update(packet.encrypted) + decryptor.finalize()
    try:
        packet.decoded.ParseFromString(raw_proto)
    except DecodeError:
        pass


async def get_topic_envelopes(topics):
    while True:
        try:
            async with aiomqtt.Client(
                "192.168.10.52"
            ) as client:
                for topic in topics:
                    await client.subscribe(topic)
                async for msg in client.messages:
                    try:
                        envelope = ServiceEnvelope.FromString(msg.payload)
                        print(envelope)
                    except DecodeError:
                        continue
                    decrypt(envelope.packet)
                    if not envelope.packet.decoded:
                        continue
                    yield msg.topic.value, envelope
        except aiomqtt.MqttError as e:
            await asyncio.sleep(1)
