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
    site = web.TCPSite(runner, bind, 80)
    await site.start()
