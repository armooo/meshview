from aiohttp import web


async def redirect(request):
    return web.Response(
        status=307,
        headers={"location": "https://meshview.armooo.net" + str(request.rel_url)},
    )


async def run_server(bind, path):
    app = web.Application()
    app.add_routes(
        [
            web.static("/.well-known/acme-challenge", path),
            web.get("/{tail:.*}", redirect),
        ]
    )
    runner = web.AppRunner(app)
    await runner.setup()
    for host in bind:
        site = web.TCPSite(runner, host, 80)
    await site.start()
