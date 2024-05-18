import asyncio

from dataclasses import dataclass
import datetime
from aiohttp_sse import sse_response
import ssl
import re

from aiohttp import web
from markupsafe import Markup
from jinja2 import Environment, PackageLoader, select_autoescape
from google.protobuf import text_format
from google.protobuf.message import Message

from meshtastic.portnums_pb2 import PortNum
from meshview import store
from meshview import models
from meshview import decode_payload
from meshview import notify

env = Environment(loader=PackageLoader("meshview"), autoescape=select_autoescape())


def node_id_to_hex(node_id):
    if node_id == 4294967295:
        return "^all"
    else:
        return f"!{hex(node_id)[2:]}"


def format_timestamp(timestamp):
    if isinstance(timestamp, int):
        timestamp = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)
    return timestamp.isoformat(timespec="seconds")


env.filters["node_id_to_hex"] = node_id_to_hex
env.filters["format_timestamp"] = format_timestamp


routes = web.RouteTableDef()


@routes.get("/")
async def index(request):
    template = env.get_template("index.html")
    return web.Response(
        text=template.render(is_hx_request="HX-Request" in request.headers, node=None),
        content_type="text/html",
    )


@dataclass
class Packet:
    id: int
    from_node_id: int
    from_node: models.Node
    to_node_id: int
    to_node: models.Node
    data: str
    raw_payload: object
    payload: str
    pretty_payload: Markup
    import_time: datetime.datetime

    @classmethod
    def from_model(cls, packet):
        mesh_packet, payload = decode_payload.decode(packet)

        pretty_payload = None

        if mesh_packet:
            mesh_packet.decoded.payload = b""
            text_mesh_packet = text_format.MessageToString(mesh_packet)
        else:
            text_mesh_packet = "Did node decode"

        if payload is None:
            text_payload = "Did not decode"
        elif isinstance(payload, Message):
            text_payload = text_format.MessageToString(payload)
        elif (
            packet.portnum == PortNum.TEXT_MESSAGE_APP
            and packet.to_node_id != 0xFFFFFFFF
        ):
            text_payload = "<redacted>"
        else:
            text_payload = payload

        if payload:
            if (
                packet.portnum == PortNum.POSITION_APP
                and payload.latitude_i
                and payload.longitude_i
            ):
                pretty_payload = Markup(
                    f'<a href="https://www.google.com/maps/search/?api=1&query={payload.latitude_i * 1e-7},{payload.longitude_i * 1e-7}" target="_blank">map</a>'
                )

        return cls(
            id=packet.id,
            from_node=packet.from_node,
            from_node_id=packet.from_node_id,
            to_node=packet.to_node,
            to_node_id=packet.to_node_id,
            data=text_mesh_packet,
            payload=text_payload,
            pretty_payload=pretty_payload,
            import_time=packet.import_time,
            raw_payload=payload,
        )


def generate_responce(request, body, raw_node_id="", node=None):
    if "HX-Request" in request.headers:
        return web.Response(text=body, content_type="text/html")

    template = env.get_template("index.html")
    return web.Response(
        text=template.render(
            is_hx_request="HX-Request" in request.headers,
            raw_node_id=raw_node_id,
            node_html=Markup(body),
            node=node,
        ),
        content_type="text/html",
    )


@routes.get("/node_search")
async def node_search(request):
    if not "q" in request.query or not request.query["q"]:
        return web.Response(text="Bad node id")
    portnum = request.query.get("portnum")
    if portnum:
        portnum = int(portnum)
    raw_node_id = request.query["q"]

    node_id = None
    if raw_node_id == "^all":
        node_id = 0xFFFFFFFF
    elif raw_node_id[0] == "!":
        try:
            node_id = int(raw_node_id[1:], 16)
        except ValueError:
            pass
    else:
        try:
            node_id = int(raw_node_id)
        except ValueError:
            pass

    if node_id is None:
        fuzzy_nodes = list(await store.get_fuzzy_nodes(raw_node_id))
        if len(fuzzy_nodes) == 1:
            node_id = fuzzy_nodes[0].node_id

    async with asyncio.TaskGroup() as tg:
        if node_id:
            node_task = tg.create_task(store.get_node(node_id))
            packets_task = tg.create_task(store.get_packets(node_id, portnum=portnum))
            position_task = tg.create_task(store.get_position(node_id))
        else:
            loop = asyncio.get_running_loop()
            node_task = loop.create_future()
            node_task.set_result(None)
            packets_task = loop.create_future()
            packets_task.set_result(())
            position_task = loop.create_future()
            position_task.set_result(None)

        node_options_task = tg.create_task(store.get_fuzzy_nodes(raw_node_id))

    packets = (Packet.from_model(p) for p in packets_task.result())
    template = env.get_template("node.html")
    options = list(node_options_task.result())
    position = Packet.from_model(position_task.result()) if position_task.result() else None

    return web.Response(
        text=template.render(
            raw_node_id=raw_node_id,
            node_id=node_id,
            node=node_task.result(),
            packets=packets,
            packet_event="packet",
            node_options=options,
            portnum=portnum,
            position=position,
        ),
        content_type="text/html",
    )


@routes.get("/node_match")
async def node_match(request):
    if not "q" in request.query or not request.query["q"]:
        return web.Response(text="Bad node id")
    raw_node_id = request.query["q"]
    node_options = await store.get_fuzzy_nodes(raw_node_id)

    template = env.get_template("datalist.html")
    return web.Response(
        text=template.render(
            node_options=node_options,
        ),
        content_type="text/html",
    )


@routes.get("/packet_list/{node_id}")
async def packet_list(request):
    node_id = int(request.match_info["node_id"])
    raw_packets = await store.get_packets(node_id)
    position = await store.get_position(node_id)
    packets = (Packet.from_model(p) for p in raw_packets)

    template = env.get_template("node.html")
    node = await store.get_node(node_id)
    return web.Response(
        text=template.render(
            raw_node_id=node_id_to_hex(node_id),
            node_id=node_id,
            node=node,
            packets=packets,
            packet_event="packet",
            position=Packet.from_model(position) if position else None,
        ),
        content_type="text/html",
    )


@routes.get("/uplinked_list/{node_id}")
async def uplinked_list(request):
    node_id = int(request.match_info["node_id"])
    raw_packets = await store.get_uplinked_packets(node_id)
    packets = (Packet.from_model(p) for p in raw_packets)
    position = await store.get_position(node_id)

    node = await store.get_node(node_id)
    template = env.get_template("node.html")
    node_html = template.render()
    return web.Response(
        text=template.render(
            raw_node_id=node_id_to_hex(node_id),
            node_id=node_id,
            node=node,
            packets=packets,
            packet_event="uplinked",
            position=Packet.from_model(position) if position else None,
        ),
        content_type="text/html",
    )


@routes.get("/chat_events")
async def chat_events(request):
    chat_packet = env.get_template("chat_packet.html")

    with notify.subscribe(node_id=0xFFFFFFFF) as event:
        async with sse_response(request) as resp:
            while resp.is_connected():
                try:
                    async with asyncio.timeout(10):
                        await event.wait()
                except TimeoutError:
                    continue
                if event.is_set():
                    packets = [
                        p
                        for p in event.packets
                        if PortNum.TEXT_MESSAGE_APP == p.portnum
                    ]
                    event.clear()
                    try:
                        for packet in packets:
                            ui_packet = Packet.from_model(packet)
                            if not re.match(r"seq \d+$", ui_packet.payload):
                                await resp.send(
                                    chat_packet.render(
                                        packet=ui_packet,
                                    ),
                                    event="chat_packet",
                                )
                    except ConnectionResetError:
                        return


@routes.get("/events")
async def events(request):
    node_id = request.query.get("node_id")
    if node_id:
        node_id = int(node_id)
    portnum = request.query.get("portnum")
    if portnum:
        portnum = int(portnum)

    packet_template = env.get_template("packet.html")
    with notify.subscribe(node_id) as event:
        async with sse_response(request) as resp:
            while resp.is_connected():
                try:
                    async with asyncio.timeout(10):
                        await event.wait()
                except TimeoutError:
                    continue
                if event.is_set():
                    packets = [
                        p
                        for p in event.packets
                        if portnum is None or portnum == p.portnum
                    ]
                    uplinked = [
                        u
                        for u in event.uplinked
                        if portnum is None or portnum == p.portnum
                    ]
                    event.clear()
                    try:
                        for packet in packets:
                            await resp.send(
                                packet_template.render(
                                    is_hx_request="HX-Request" in request.headers,
                                    node_id=node_id,
                                    packet=Packet.from_model(packet),
                                ),
                                event="packet",
                            )
                        for packet in uplinked:
                            await resp.send(
                                packet_template.render(
                                    is_hx_request="HX-Request" in request.headers,
                                    node_id=node_id,
                                    packet=Packet.from_model(packet),
                                ),
                                event="uplinked",
                            )
                    except ConnectionResetError:
                        return


@routes.get("/packet_details/{packet_id}")
async def packet_details(request):
    packets_seen = await store.get_packets_seen(int(request.match_info["packet_id"]))
    template = env.get_template("packet_details.html")
    return web.Response(
        text=template.render(
            is_hx_request="HX-Request" in request.headers, packets_seen=packets_seen
        ),
        content_type="text/html",
    )


@routes.get("/firehose")
async def packet_details(request):
    portnum = request.query.get("portnum")
    if portnum:
        portnum = int(portnum)
    packets = await store.get_packets(portnum=portnum)
    template = env.get_template("firehose.html")
    return web.Response(
        text=template.render(
            packets=(Packet.from_model(p) for p in packets),
            portnum=portnum,
        ),
        content_type="text/html",
    )


@routes.get("/chat")
async def chat(request):
    packets = await store.get_packets(
        node_id=0xFFFFFFFF, portnum=PortNum.TEXT_MESSAGE_APP
    )
    template = env.get_template("chat.html")
    ui_packets = (Packet.from_model(p) for p in packets)
    return web.Response(
        text=template.render(
            packets=(p for p in ui_packets if not re.match(r"seq \d+$", p.payload)),
        ),
        content_type="text/html",
    )


@routes.get("/packet/{packet_id}")
async def packet(request):
    packet = await store.get_packet(int(request.match_info["packet_id"]))
    template = env.get_template("packet_index.html")
    return web.Response(
        text=template.render(packet=Packet.from_model(packet)),
        content_type="text/html",
    )


async def run_server(bind, port, tls_cert):
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    if tls_cert:
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(tls_cert)
    else:
        ssl_context = None
    for host in bind:
        site = web.TCPSite(runner, host, port, ssl_context=ssl_context)
        await site.start()

    while True:
        await asyncio.sleep(3600)  # sleep forever
