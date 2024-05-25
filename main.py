import asyncio
import argparse

from meshview import mqtt_reader
from meshview import database
from meshview import store
from meshview import web
from meshview import http


async def load_database_from_mqtt(topic):
    async for topic, env in mqtt_reader.get_topic_envelopes(topic):
        await store.process_envelope(topic, env)


async def main(args):
    database.init_database(args.database)

    await database.create_tables()
    async with asyncio.TaskGroup() as tg:
        tg.create_task(load_database_from_mqtt(args.topic))
        tg.create_task(web.run_server(args.bind, args.port, args.tls_cert))
        if args.acme_challenge:
            tg.create_task(http.run_server(args.bind, args.acme_challenge))


if __name__ == '__main__':
    parser = argparse.ArgumentParser('meshview')
    parser.add_argument('--bind', nargs='*', default=['::1'])
    parser.add_argument('--acme-challenge')
    parser.add_argument('--port', default=8080, type=int)
    parser.add_argument('--tls-cert')

    parser.add_argument('--topic', nargs='*', default=['msh/US/bayarea/#'])

    parser.add_argument('--database', default='sqlite+aiosqlite:///packets.db')

    args = parser.parse_args()

    asyncio.run(main(args))
