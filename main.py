import asyncio

from meshview import web


async def main():
    async with asyncio.TaskGroup() as tg:
        tg.create_task(web.run_server())


if __name__ == '__main__':
    asyncio.run(main())
