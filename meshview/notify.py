import asyncio
import contextlib
from collections import defaultdict

waiting_node_ids_events = defaultdict(set)


class NodeEvent:
    def __init__(self, node_id):
        self.node_id = node_id
        self.event = asyncio.Event()
        self.packets = []
        self.uplinked = []

    async def wait(self):
        await self.event.wait()

    def set(self):
        return self.event.set()

    def is_set(self):
        return self.event.is_set()

    def clear(self):
        del self.packets[:]
        del self.uplinked[:]
        return self.event.clear()


def create_event(node_id):
    event = NodeEvent(node_id)
    waiting_node_ids_events[node_id].add(event)
    return event


def remove_event(node_event):
    waiting_node_ids_events[node_event.node_id].remove(node_event)


def notify_packet(node_id, packet):
    for event in waiting_node_ids_events[node_id]:
        event.packets.append(packet)
        event.set()


def notify_uplinked(node_id, packet):
    for event in waiting_node_ids_events[node_id]:
        event.uplinked.append(packet)
        event.set()


@contextlib.contextmanager
def subscribe(node_id):
    """
    Context manager for subscribing to events for a node_id.
    Automatically manages event creation and cleanup.
    """
    event = create_event(node_id)
    try:
        yield event
    except Exception as e:
        print(f"Error during subscription for node_id={node_id}: {e}")
        raise
    finally:
        remove_event(event)
