import asyncio
import io

from dataclasses import dataclass
import datetime
from aiohttp_sse import sse_response
import ssl
import re

import pydot
from pandas import DataFrame
import seaborn as sns
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
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


async def build_trace(node_id):
    trace = []
    for raw_p in await store.get_packets_from(node_id, PortNum.POSITION_APP):
        p = Packet.from_model(raw_p)
        if not p.raw_payload.latitude_i or not p.raw_payload.longitude_i:
            continue
        trace.append((p.raw_payload.latitude_i * 1e-7, p.raw_payload.longitude_i * 1e-7))
    return trace


async def build_neighbors(node_id):
    packet = (await store.get_packets_from(node_id, PortNum.NEIGHBORINFO_APP, 1)).first()
    if not packet:
        return []
    if packet.import_time < datetime.datetime.utcnow() - datetime.timedelta(days=1):
        return []
    _, payload = decode_payload.decode(packet)
    neighbors = []
    results = {}
    async with asyncio.TaskGroup() as tg:
        for n in payload.neighbors:
            results[n.node_id] = {
                'node_id': n.node_id,
                'snr': n.snr,
            }
            neighbors.append((n.node_id, tg.create_task(store.get_node(n.node_id))))

    for node_id, node in neighbors:
        node = await node
        if node and node.last_lat and node.last_long:
            results[node_id]['short_name'] = node.short_name
            results[node_id]['long_name'] = node.long_name
            results[node_id]['location'] = (node.last_lat * 1e-7, node.last_long * 1e-7)
        else:
            del results[node_id]

    return list(results.values())


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
    portnum: int
    data: str
    raw_mesh_packet: object
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
            portnum=packet.portnum,
            data=text_mesh_packet,
            payload=text_payload,
            pretty_payload=pretty_payload,
            import_time=packet.import_time,
            raw_mesh_packet=mesh_packet,
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

    if node_id:
        return web.Response(
            status=307,
            headers={'Location': f'/packet_list/{node_id}?{request.query_string}'},
        )

    template = env.get_template("search.html")
    return web.Response(
        text=template.render(
            nodes=fuzzy_nodes,
            query_string=request.query_string,
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
    if portnum := request.query.get("portnum"):
        portnum = int(portnum)
    else:
        portnum = None
    return await _packet_list(request, store.get_packets(node_id, portnum), 'packet')


@routes.get("/uplinked_list/{node_id}")
async def uplinked_list(request):
    node_id = int(request.match_info["node_id"])
    if portnum := request.query.get("portnum"):
        portnum = int(portnum)
    else:
        portnum = None
    return await _packet_list(request, store.get_uplinked_packets(node_id, portnum), 'uplinked')


async def _packet_list(request, raw_packets, packet_event):
    node_id = int(request.match_info["node_id"])
    if portnum := request.query.get("portnum"):
        portnum = int(portnum)
    else:
        portnum = None

    async with asyncio.TaskGroup() as tg:
        raw_packets = tg.create_task(raw_packets)
        node = tg.create_task(store.get_node(node_id))
        trace = tg.create_task(build_trace(node_id))
        neighbors = tg.create_task(build_neighbors(node_id))
        has_telemetry = tg.create_task(store.has_packets(node_id, PortNum.TELEMETRY_APP))

    packets = (Packet.from_model(p) for p in await raw_packets)

    template = env.get_template("node.html")
    return web.Response(
        text=template.render(
            raw_node_id=node_id_to_hex(node_id),
            node_id=node_id,
            node=await node,
            portnum=portnum,
            packets=packets,
            packet_event=packet_event,
            trace=await trace,
            neighbors=await neighbors,
            has_telemetry=await has_telemetry,
            query_string=request.query_string,
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
                        if portnum is None or portnum == u.portnum
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


@routes.get("/graph/power/{node_id}")
async def graph_power(request):
    date = []
    battery = []
    voltage = []
    for p in await store.get_packets_from(int(request.match_info['node_id']), PortNum.TELEMETRY_APP):
        _, payload = decode_payload.decode(p)
        if not payload:
            continue
        if not payload.HasField('device_metrics'):
            continue
        timestamp = datetime.datetime.fromtimestamp(payload.time)
        if timestamp.year < 2000:
            continue
        date.append(timestamp)
        battery.append(payload.device_metrics.battery_level)
        voltage.append(payload.device_metrics.voltage)


    if not date:
        return web.Response(
            status=404,
            content_type="image/png",
        )


    max_time = datetime.timedelta(days=4)
    newest = date[0]
    for i, d in enumerate(date):
        if d < newest - max_time:
            break

    fig, ax1 = plt.subplots(figsize=(10, 10))
    fig.autofmt_xdate()
    ax1.set_xlabel('time')
    ax1.set_ylabel('battery level', color='tab:blue')
    ax2 = ax1.twinx()
    ax2.set_ylabel('voltage', color='tab:red')
    sns.lineplot(x=date[:i], y=battery[:i], ax=ax1, color='tab:blue')
    sns.lineplot(x=date[:i], y=voltage[:i], ax=ax2, color='tab:red')

    png = io.BytesIO()
    plt.savefig(png, dpi=100)
    plt.close()

    return web.Response(
        body=png.getvalue(),
        content_type="image/png",
    )


@routes.get("/graph/neighbors/{node_id}")
async def graph_neighbors(request):
    oldest = datetime.datetime.utcnow() - datetime.timedelta(days=4)

    data = {}
    dates =[]
    for p in await store.get_packets_from(int(request.match_info['node_id']), PortNum.NEIGHBORINFO_APP):
        _, payload = decode_payload.decode(p)
        if not payload:
            continue
        if p.import_time < oldest:
            break

        dates.append(p.import_time)
        for v in data.values():
            v.append(None)

        for n in payload.neighbors:
            data.setdefault(n.node_id, [None] * len(dates))[-1] = n.snr

    nodes = {}
    async with asyncio.TaskGroup() as tg:
        for node_id in data:
            nodes[node_id] = tg.create_task(store.get_node(node_id))

    data_by_short_name = {}
    for node_id, data in data.items():
        node = await nodes[node_id]
        if node:
            data_by_short_name[node.short_name] = data
        else:
            data_by_short_name[node_id_to_hex(node_id)] = data

    fig, ax1 = plt.subplots(figsize=(10, 10))
    ax1.set_xlabel('time')
    ax1.set_ylabel('SNR')
    df = DataFrame(data_by_short_name, index=dates)
    sns.lineplot(data=df)

    png = io.BytesIO()
    plt.savefig(png, dpi=100)
    plt.close()

    return web.Response(
        body=png.getvalue(),
        content_type="image/png",
    )


@routes.get("/graph/traceroute/{packet_id}")
async def graph_traceroute(request):
    packet_id = int(request.match_info['packet_id'])
    traceroutes = list(await store.get_traceroute(packet_id))

    packet = await store.get_packet(packet_id)

    node_ids = set()
    for tr in traceroutes:
        route = decode_payload.decode_payload(PortNum.TRACEROUTE_APP, tr.route)
        node_ids.add(tr.gateway_node_id)
        for node_id in route.route:
            node_ids.add(node_id)
    node_ids.add(packet.from_node_id)
    node_ids.add(packet.to_node_id)

    nodes = {}
    async with asyncio.TaskGroup() as tg:
        for node_id in node_ids:
            nodes[node_id] = tg.create_task(store.get_node(node_id))

    graph = pydot.Dot('traceroute', graph_type="digraph")

    paths = set()
    node_color = {}
    mqtt_nodes = set()
    saw_reply = set()
    dest = None
    node_seen_time = {}
    for tr in traceroutes:
        if tr.done:
            saw_reply.add(tr.gateway_node_id)
        if tr.done and dest:
            continue
        route = decode_payload.decode_payload(PortNum.TRACEROUTE_APP, tr.route)
        path = [packet.from_node_id]
        path.extend(route.route)
        if tr.done:
            dest = packet.to_node_id
            path.append(packet.to_node_id)
        elif path[-1] != tr.gateway_node_id:
            # It seems some nodes add them self to the list before uplinking
            path.append(tr.gateway_node_id)

        if not tr.done:
            for node_id in path[-2:]:
                if node_id not in node_seen_time:
                    if tr.import_time:
                        node_seen_time[node_id] = tr.import_time

        mqtt_nodes.add(tr.gateway_node_id)
        node_color[path[-1]] = '#' + hex(hash(tuple(path)))[3:9]
        paths.add(tuple(path))

    used_nodes = set()
    for path in paths:
        used_nodes.update(path)

    import_times = [tr.import_time for tr in traceroutes if tr.import_time]
    if import_times:
        first_time = min(import_times)
    else:
        first_time = 0

    for node_id in used_nodes:
        node = await nodes[node_id]
        if not node:
            node_name = node_id_to_hex(node_id)
        else:
            node_name = f'[{node.short_name}] {node.long_name} - {node_id_to_hex(node_id)}'
        if node_id in node_seen_time:
            ms = (node_seen_time[node_id] - first_time).total_seconds() * 1000
            node_name += f'\n {ms:.2f}ms'
        style = 'dashed'
        if node_id == dest:
            style = 'filled'
        elif node_id in mqtt_nodes:
            style = 'solid'

        if node_id in saw_reply:
            style += ', diagonals'

        graph.add_node(pydot.Node(
            str(node_id),
            label=node_name,
            shape='box',
            color=node_color.get(node_id, 'black'),
            style=style,
        ))

    for path in paths:
        color = '#' + hex(hash(tuple(path)))[3:9]
        for src, dest in zip(path, path[1:]):
            graph.add_edge(pydot.Edge(src, dest, color=color))

    return web.Response(
        body=graph.create_svg(),
        content_type="image/svg+xml",
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
