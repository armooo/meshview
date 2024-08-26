import asyncio
import io
from collections import Counter
from dataclasses import dataclass
import datetime
from aiohttp_sse import sse_response
import ssl
import re
import os

import pydot
from pandas import DataFrame
import plotly.express as px
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


with open(os.path.join(os.path.dirname(__file__), '1x1.png'), 'rb') as png:
    empty_png = png.read()


env = Environment(loader=PackageLoader("meshview"), autoescape=select_autoescape())


async def build_trace(node_id):
    trace = []
    for raw_p in await store.get_packets_from(node_id, PortNum.POSITION_APP, since=datetime.timedelta(hours=24)):
        p = Packet.from_model(raw_p)
        if not p.raw_payload or not p.raw_payload.latitude_i or not p.raw_payload.longitude_i:
            continue
        trace.append((p.raw_payload.latitude_i * 1e-7, p.raw_payload.longitude_i * 1e-7))
    if not trace:
        for raw_p in await store.get_packets_from(node_id, PortNum.POSITION_APP):
            p = Packet.from_model(raw_p)
            if not p.raw_payload or not p.raw_payload.latitude_i or not p.raw_payload.longitude_i:
                continue
            trace.append((p.raw_payload.latitude_i * 1e-7, p.raw_payload.longitude_i * 1e-7))
            break
    return trace


async def build_neighbors(node_id):
    packet = (await store.get_packets_from(node_id, PortNum.NEIGHBORINFO_APP, limit=1)).first()
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
    return timestamp.isoformat(timespec="milliseconds")


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
    net_packet_template = env.get_template("net_packet.html")
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
                            ui_packet = Packet.from_model(packet)
                            await resp.send(
                                packet_template.render(
                                    is_hx_request="HX-Request" in request.headers,
                                    node_id=node_id,
                                    packet=ui_packet,
                                ),
                                event="packet",
                            )
                            if ui_packet.portnum == PortNum.TEXT_MESSAGE_APP and '#baymeshnet' in ui_packet.payload.lower():
                                await resp.send(
                                    net_packet_template.render(packet=ui_packet),
                                    event="net_packet",
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

@dataclass
class UplinkedNode:
    lat: float
    long: float
    long_name: str
    short_name: str
    hops: int
    snr: float
    rssi: float


@routes.get("/packet_details/{packet_id}")
async def packet_details(request):
    packet_id = int(request.match_info["packet_id"])
    packets_seen = list(await store.get_packets_seen(packet_id))
    packet = await store.get_packet(packet_id)

    from_node_cord = None
    if packet.from_node and packet.from_node.last_lat:
        from_node_cord = [packet.from_node.last_lat * 1e-7 , packet.from_node.last_long * 1e-7]

    uplinked_nodes = []
    for p in packets_seen:
        if p.node and p.node.last_lat:
            if p.topic.startswith('mqtt-meshtastic-org'):
                hops = 666
            else:
                hops = p.hop_start - p.hop_limit
            uplinked_nodes.append(
                UplinkedNode(
                    lat=p.node.last_lat * 1e-7,
                    long=p.node.last_long * 1e-7,
                    long_name=p.node.long_name,
                    short_name=p.node.short_name,
                    hops=hops,
                    snr=p.rx_snr,
                    rssi=p.rx_rssi,
                )
            )

    map_center = None
    if from_node_cord:
        map_center = from_node_cord
    elif uplinked_nodes:
        map_center = [uplinked_nodes[0].lat, uplinked_nodes[0].long]

    template = env.get_template("packet_details.html")
    return web.Response(
        text=template.render(
            packets_seen=packets_seen,
            map_center=map_center,
            from_node_cord=from_node_cord,
            uplinked_nodes=uplinked_nodes,
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
    if not packet:
        return web.Response(
            status=404,
        )
    template = env.get_template("packet_index.html")
    return web.Response(
        text=template.render(packet=Packet.from_model(packet)),
        content_type="text/html",
    )


async def graph_telemetry(node_id, payload_type, graph_config):
    data = {'date': []}
    fields = []
    for c in graph_config:
        fields.extend(c['fields'])

    for field in fields:
        data[field] = []

    for p in await store.get_packets_from(node_id, PortNum.TELEMETRY_APP):
        _, payload = decode_payload.decode(p)
        if not payload:
            continue
        if not payload.HasField(payload_type):
            continue
        data_field = getattr(payload, payload_type)
        timestamp = p.import_time
        data['date'].append(timestamp)
        for field in fields:
            data[field].append(getattr(data_field, field))

    if not data['date']:
        return web.Response(
            body=empty_png,
            status=404,
            content_type="image/png",
        )

    max_time = datetime.timedelta(days=4)
    newest = data['date'][0]
    for i, d in enumerate(data['date']):
        if d < newest - max_time:
            break

    fig, ax = plt.subplots(figsize=(10, 10))
    fig.autofmt_xdate()
    ax.set_xlabel('time')
    axes = {0: ax}

    date = data.pop('date')
    df = DataFrame(data, index=date)

    for i, ax_config in enumerate(graph_config):
        args = {}
        if 'color' in ax_config:
            args['color'] =  'tab:' + ax_config['color']
        if i:
            ax = ax.twinx()
        ax.set_ylabel(ax_config['label'], **args)
        ax_df = df[ax_config['fields']]
        args = {}
        if 'palette' in ax_config:
            args['palette'] = ax_config['palette']
        sns.lineplot(data=ax_df, ax=ax, **args)

    png = io.BytesIO()
    plt.savefig(png, dpi=100)
    plt.close()

    return web.Response(
        body=png.getvalue(),
        content_type="image/png",
    )


@routes.get("/graph/power/{node_id}")
async def graph_power(request):
    return await graph_telemetry(
        int(request.match_info['node_id']),
        'device_metrics',
        [
            {
                'label': 'battery level',
                'fields': ['battery_level'],
            },
            {
                'label': 'voltage',
                'fields': ['voltage'],
                'palette': 'Set2',
            },
        ],
    )


@routes.get("/graph/chutil/{node_id}")
async def graph_chutil(request):
    return await graph_telemetry(
        int(request.match_info['node_id']),
        'device_metrics',
        [
            {
                'label': 'utilization',
                'fields': ['channel_utilization', 'air_util_tx'],
            },
        ],
    )




@routes.get("/graph/wind_speed/{node_id}")
async def graph_wind_speed(request):
    return await graph_telemetry(
        int(request.match_info['node_id']),
        'environment_metrics',
        [
            {
                'label': 'wind speed m/s',
                'fields': ['wind_speed'],
            },
        ],
    )


@routes.get("/graph/wind_direction/{node_id}")
async def graph_wind_direction(request):
    return await graph_telemetry(
        int(request.match_info['node_id']),
        'environment_metrics',
        [
            {
                'label': 'wind direction',
                'fields': ['wind_direction'],
            },
        ],
    )

@routes.get("/graph/temperature/{node_id}")
async def graph_temperature(request):
    return await graph_telemetry(
        int(request.match_info['node_id']),
        'environment_metrics',
        [
            {
                'label': 'temperature C',
                'fields': ['temperature'],
            },
        ],
    )


@routes.get("/graph/humidity/{node_id}")
async def graph_humidity(request):
    return await graph_telemetry(
        int(request.match_info['node_id']),
        'environment_metrics',
        [
            {
                'label': 'humidity',
                'fields': ['relative_humidity'],
            },
        ],
    )


@routes.get("/graph/power_metrics/{node_id}")
async def graph_power_metrics(request):
    return await graph_telemetry(
        int(request.match_info['node_id']),
        'power_metrics',
        [
            {
                'label': 'voltage',
                'fields': ['ch1_voltage', 'ch2_voltage', 'ch3_voltage'],
            },
            {
                'label': 'current',
                'fields': ['ch1_current', 'ch2_current', 'ch3_current'],
                'palette': 'Set2',
            },
        ],
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

    fig, ax1 = plt.subplots(figsize=(5, 5))
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

@routes.get("/graph/neighbors2/{node_id}")
async def graph_neighbors2(request):
    oldest = datetime.datetime.utcnow() - datetime.timedelta(days=30)

    data = []
    node_ids = set()
    for p in await store.get_packets_from(int(request.match_info['node_id']), PortNum.NEIGHBORINFO_APP):
        _, payload = decode_payload.decode(p)
        if not payload:
            continue
        if p.import_time < oldest:
            break

        for n in payload.neighbors:
            node_ids.add(n.node_id)
            data.append({
                'time': p.import_time,
                'snr': n.snr,
                'node_id': n.node_id,
            })

    nodes = {}
    async with asyncio.TaskGroup() as tg:
        for node_id in node_ids:
            nodes[node_id] = tg.create_task(store.get_node(node_id))

    for d in data:
        node = await nodes[d['node_id']]
        if node:
            d['node_name'] = node.short_name
        else:
            d['node_name'] = node_id_to_hex(node_id)

    df = DataFrame(data)
    fig = px.line(df, x="time", y="snr", color="node_name", markers=True)
    html = fig.to_html(full_html=True, include_plotlyjs='cdn')
    return web.Response(
        text=html,
        content_type="text/html",
    )


@routes.get("/graph/traceroute/{packet_id}")
async def graph_traceroute(request):
    packet_id = int(request.match_info['packet_id'])
    traceroutes = list(await store.get_traceroute(packet_id))

    packet = await store.get_packet(packet_id)
    if not packet:
        return web.Response(
            status=404,
        )

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

        if not tr.done and tr.gateway_node_id not in node_seen_time and tr.import_time:
            node_seen_time[path[-1]] = tr.import_time

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
            node_name = f'[{node.short_name}] {node.long_name}\n{node_id_to_hex(node_id)}\n{node.role}'
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
            href=f"/packet_list/{node_id}",
        ))

    for path in paths:
        color = '#' + hex(hash(tuple(path)))[3:9]
        for src, dest in zip(path, path[1:]):
            graph.add_edge(pydot.Edge(src, dest, color=color))

    return web.Response(
        body=graph.create_svg(),
        content_type="image/svg+xml",
    )

@routes.get("/graph/network")
async def graph_network(request):
    root = request.query.get("root")
    depth = int(request.query.get("depth", 5))
    hours = int(request.query.get("hours", 24))
    minutes = int(request.query.get("minutes", 0))
    since = datetime.timedelta(hours=hours, minutes=minutes)

    nodes = {}
    node_ids = set()

    traceroutes = []
    for tr in await store.get_traceroutes(since):
        node_ids.add(tr.gateway_node_id)
        node_ids.add(tr.packet.from_node_id)
        node_ids.add(tr.packet.to_node_id)
        route = decode_payload.decode_payload(PortNum.TRACEROUTE_APP, tr.route)
        node_ids.update(route.route)

        path = [tr.packet.from_node_id]
        path.extend(route.route)
        if tr.done:
            path.append(tr.packet.to_node_id)
        else:
            if path[-1] != tr.gateway_node_id:
                # It seems some nodes add them self to the list before uplinking
                path.append(tr.gateway_node_id)
        traceroutes.append((tr, path))

    edges = Counter()
    edge_type = {}
    used_nodes = set()

    for ps, p in await store.get_mqtt_neighbors(since):
        node_ids.add(ps.node_id)
        node_ids.add(p.from_node_id)
        used_nodes.add(ps.node_id)
        used_nodes.add(p.from_node_id)
        edges[(p.from_node_id, ps.node_id)] += 1
        edge_type[(p.from_node_id, ps.node_id)] = 'sni'

    for packet in await store.get_packets(
        portnum=PortNum.NEIGHBORINFO_APP,
        since=since,
    ):
        _, neighbor_info = decode_payload.decode(packet)
        node_ids.add(packet.from_node_id)
        used_nodes.add(packet.from_node_id)
        for node in neighbor_info.neighbors:
            node_ids.add(node.node_id)
            used_nodes.add(node.node_id)
            edges[(node.node_id, packet.from_node_id)] += 1
            edge_type[(node.node_id, packet.from_node_id)] = 'ni'

    async with asyncio.TaskGroup() as tg:
        for node_id in node_ids:
            nodes[node_id] = tg.create_task(store.get_node(node_id))

    tr_done = set()
    for tr, path in traceroutes:
        if tr.done:
            if tr.packet_id in tr_done:
                continue
            else:
                tr_done.add(tr.packet_id)

        for src, dest in zip(path, path[1:]):
            used_nodes.add(src)
            used_nodes.add(dest)
            edges[(src, dest)] += 1
            edge_type[(src, dest)] = 'tr'

    async def get_node_name(node_id):
        node = await nodes[node_id]
        if not node:
            node_name = node_id_to_hex(node_id)
        else:
            node_name = f'[{node.short_name}] {node.long_name}\n{node_id_to_hex(node_id)}'
        return node_name

    if root:
        new_used_nodes = set()
        new_edges = Counter()
        edge_map = {}
        for src, dest in edges:
            edge_map.setdefault(dest, []).append(src)

        queue = [int(root)]
        for i in range(depth):
            next_queue = []
            for node in queue:
                new_used_nodes.add(node)
                for dest in edge_map.get(node, []):
                    new_used_nodes.add(dest)
                    new_edges[(dest, node)] += 1
                    next_queue.append(dest)
            queue = next_queue

        used_nodes = new_used_nodes
        edges = new_edges

    #graph = pydot.Dot('network', graph_type="digraph", layout="sfdp", overlap="prism", quadtree="2", repulsiveforce="1.5", k="1", overlap_scaling="1.5", concentrate=True)
    #graph = pydot.Dot('network', graph_type="digraph", layout="sfdp", overlap="prism1000", overlap_scaling="-4", sep="1000", pack="true")
    graph = pydot.Dot('network', graph_type="digraph", layout="neato", overlap="false", model='subset', esep="+5")
    for node_id in used_nodes:
        node = await nodes[node_id]
        color = '#000000'
        node_name = await get_node_name(node_id)
        if node and node.role in ('ROUTER', 'ROUTER_CLIENT', 'REPEATER'):
            color = '#0000FF'
        elif node and node.role == 'CLIENT_MUTE':
            color = '#00FF00'
        graph.add_node(pydot.Node(
            str(node_id),
            label=node_name,
            shape='box',
            color=color,
            href=f"/graph/network?root={node_id}&amp;depth={depth-1}",
        ))

    if edges:
        max_edge_count = edges.most_common(1)[0][1]
    else:
        max_edge_count = 1

    size_ratio = 2. / max_edge_count


    edge_added = set()

    for (src, dest), edge_count in edges.items():
        size = max(size_ratio * edge_count, .25)
        arrowsize = max(size_ratio * edge_count, .5)
        if edge_type[(src, dest)] in ('ni'):
            color = '#FF0000'
        elif  edge_type[(src, dest)] in ('sni'):
            color = '#00FF00'
        else:
            color = '#000000'
        edge_dir = "forward"
        if (dest, src) in edges and edge_type[(src, dest)] == edge_type[(dest, src)]:
            edge_dir = "both"
            edge_added.add((dest, src))

        if (src, dest) not in edge_added:
            edge_added.add((src, dest))
            graph.add_edge(pydot.Edge(
                str(src),
                str(dest),
                color=color,
                tooltip=f'{await get_node_name(src)} -> {await get_node_name(dest)}',
                penwidth=1.85,
                dir=edge_dir,
            ))

    return web.Response(
        body=graph.create_svg(),
        content_type="image/svg+xml",
    )


@routes.get("/net")
async def net(request):
    if "date" in request.query:
        start_date = datetime.date.fromisoformat(request.query["date"])
    else:
        start_date = datetime.date.today()
        while start_date.weekday() != 2:
            start_date = start_date - datetime.timedelta(days=1)

    start_time = datetime.datetime.combine(start_date, datetime.time(0,0))

    text_packets = [
        Packet.from_model(p)
        for p in await store.get_packets(
            portnum=PortNum.TEXT_MESSAGE_APP,
            after=start_time,
            before=start_time + datetime.timedelta(hours=1),
        )
    ]
    net_packets = [p for p in text_packets if '#baymeshnet' in p.payload.lower()]

    template = env.get_template("net.html")
    return web.Response(
        text=template.render(net_packets=net_packets),
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
