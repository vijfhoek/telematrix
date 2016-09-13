import json
import requests
import threading
import aiotg
import asyncio
import logging
import mimetypes
import time
from urllib.parse import unquote, quote, urlparse, parse_qs
from aiohttp import web, ClientSession, MultipartWriter
from pprint import pprint
from bs4 import BeautifulSoup

try:
    with open('config.json', 'r') as f:
        config = json.load(f)

        HS_TOKEN = config['tokens']['hs']
        AS_TOKEN = config['tokens']['as']
        TG_TOKEN = config['tokens']['telegram']
        GOOGLE_TOKEN = config['tokens']['google']

        MATRIX_HOST = config['hosts']['internal']
        MATRIX_HOST_EXT = config['hosts']['external']

        MATRIX_PREFIX = MATRIX_HOST + '_matrix/client/r0/'
        MATRIX_MEDIA_PREFIX = MATRIX_HOST + '_matrix/media/r0/'

        USER_ID_FORMAT = config['user_id_format']

        telegram_chats = config['chats']
        matrix_rooms = {v: k for k, v in telegram_chats.items()}
except (OSError, IOError) as e:
    print('Error opening config file:')
    print(e)
    exit(1)

bot = aiotg.Bot(api_token=TG_TOKEN)
client_session = ClientSession()

def create_response(code, obj):
    return web.Response(text=json.dumps(obj), status=code, content_type='application/json', charset='utf-8')

VALID_TAGS = ['b', 'strong', 'i', 'em', 'a', 'pre']
def sanitize_html(h):
    h = h.replace('\n', '')
    h = h.replace('<br>', '\n').replace('<br/>', '\n').replace('<br />', '\n')
    soup = BeautifulSoup(h, 'html.parser')
    for tag in soup.find_all(True):
        if tag.name == 'blockquote':
            tag.string = ('\n' + tag.text).replace('\n', '\n> ').rstrip('\n>')
        if tag.name not in VALID_TAGS:
            tag.hidden = True
    return soup.renderContents().decode('utf-8')


async def matrix_transaction(request):
    transaction = request.match_info['transaction']
    body = await request.json()

    events = body['events']
    for event in events:
        room_id = event['room_id']
        if room_id not in matrix_rooms:
            print('{} not in matrix_rooms!'.format(room_id))
        elif event['type'] == 'm.room.message':
            group = bot.group(matrix_rooms[room_id])

            username = event['user_id'].split(':')[0][1:]
            if username.startswith('telegram_'):
                return create_response(200, {})
            content = event['content']
            if content['msgtype'] == 'm.text':
                if 'formatted_body' in content:
                    await group.send_text('&lt;{}&gt; {}'.format(username, sanitize_html(content['formatted_body'])), parse_mode='HTML')
                else:
                    await group.send_text('<{}> {}'.format(username, content['body']))
            elif content['msgtype'] == 'm.notice':
                if 'formatted_body' in content:
                    await group.send_text('[{}] {}'.format(username, sanitize_html(content['formatted_body'])), parse_mode='HTML')
                else:
                    await group.send_text('[{}] {}'.format(username, content['body']))
            elif content['msgtype'] == 'm.emote':
                if 'formatted_body' in content:
                    await group.send_text('*** {} {}'.format(username, sanitize_html(content['formatted_body'])), parse_mode='HTML')
                else:
                    await group.send_text('*** {} {}'.format(username, content['body']))
            elif content['msgtype'] == 'm.image':
                url = urlparse(content['url'])
                async with client_session.get(MATRIX_MEDIA_PREFIX + 'download/{}{}'.format(url.netloc, url.path)) as response:
                    b = await response.read()

                with open('/tmp/{}'.format(content['body']), 'wb') as f:
                    f.write(b)
                with open('/tmp/{}'.format(content['body']), 'rb') as f:
                    url_str = MATRIX_HOST_EXT + '_matrix/media/r0/download/{}{}'.format(url.netloc, quote(url.path))
                    async with ClientSession() as shorten_session:
                        async with shorten_session.post('https://www.googleapis.com/urlshortener/v1/url',
                                                        params={'key': GOOGLE_TOKEN},
                                                        data=json.dumps({'longUrl': url_str}),
                                                        headers={'Content-Type': 'application/json'}) as response:
                            j = await response.json()

                            if 'id' in j:
                                url_str = j['id']
                            else:
                                print('Something went wrong while shortening:')
                                pprint(j)

                    caption = '<{}> {} ({})'.format(username, content['body'], url_str)
                    await group.send_photo(f, caption=caption)
            else:
                print(json.dumps(content, indent=4))

    return create_response(200, {})

async def _matrix_request(method_fun, category, path, user_id, data=None, content_type=None):
    if data is not None:
        if isinstance(data, dict):
            data = json.dumps(data)
            content_type = 'application/json; charset=utf-8'
        elif content_type is None:
            content_type = 'application/octet-stream'

    params = {'access_token': AS_TOKEN}
    if user_id is not None:
        params['user_id'] = user_id

    async with method_fun('{}_matrix/{}/r0/{}'.format(MATRIX_HOST, quote(category), quote(path)),
                          params=params, data=data, headers={'Content-Type': content_type}) as response:
        if response.headers['Content-Type'].split(';')[0] == 'application/json':
            return await response.json()
        else:
            return await response.read()

def matrix_post(category, path, user_id, data, content_type=None):
    return _matrix_request(client_session.post, category, path, user_id, data, content_type)

def matrix_put(category, path, user_id, data, content_type=None):
    return _matrix_request(client_session.put, category, path, user_id, data, content_type)

def matrix_get(category, path, user_id):
    return _matrix_request(client_session.get, category, path, user_id)

def matrix_delete(category, path, user_id):
    return _matrix_request(client_session.delete, category, path, user_id)

async def matrix_room(request):
    room_alias = request.match_info['room_alias']
    args = parse_qs(urlparse(request.path_qs).query)
    print('Checking for {} | {}'.format(unquote(room_alias), args['access_token'][0]))

    try:
        if args['access_token'][0] != HS_TOKEN:
            return create_response(403, {'errcode': 'M_FORBIDDEN'})
    except KeyError:
        return create_response(401, {'errcode': 'NL.SIJMENSCHOON.TELEMATRIX_UNAUTHORIZED'})

    localpart, host = room_alias.split(':')
    chat = '_'.join(localpart.split('_')[1:])

    if chat in telegram_chats:
        await matrix_post('client', 'createRoom', None, {'room_alias_name': localpart[1:]})
        return create_response(200, {})
    else:
        return create_response(404, {'errcode': 'NL.SIJMENSCHOON.TELEMATRIX_NOT_FOUND'})

def send_matrix_message(room_id, user_id, txn_id, **kwargs):
    return matrix_put('client', 'rooms/{}/send/m.room.message/{}'.format(room_id, txn_id), user_id, kwargs)

async def upload_tgfile_to_matrix(file_id, user_id):
    file_path = (await bot.get_file(file_id))['file_path']
    request = await bot.download_file(file_path)
    mimetype = request.headers['Content-Type']

    data = await request.read()
    j = await matrix_post('media', 'upload', user_id, data, mimetype)

    if 'content_uri' in j:
        return (j['content_uri'], mimetype, len(data))
    else:
        return None, None, 0

async def register_join_matrix(chat, room_id, user_id):
    name = chat.sender['first_name']
    if 'last_name' in chat.sender:
        name += ' ' + chat.sender['last_name']
    name += ' (Telegram)'
    user = user_id.split(':')[0][1:]

    await matrix_post('client', 'register', None, {'type': 'm.login.application_service', 'user': user})
    profile_photos = await bot.get_user_profile_photos(chat.sender['id'])
    try:
        pp_file_id = profile_photos['result']['photos'][0][-1]['file_id']
        pp_uri, _, _ = await upload_tgfile_to_matrix(pp_file_id, user_id)
        if pp_uri:
            await matrix_put('client', 'profile/{}/avatar_url'.format(user_id), user_id, {'avatar_url': pp_uri})
    except IndexError:
        pass

    await matrix_put('client', 'profile/{}/displayname'.format(user_id), user_id, {'displayname': name})
    await matrix_post('client', 'join/{}'.format(room_id), user_id, {})

@bot.handle('photo')
async def aiotg_photo(chat, photo):
    try:
        room_id = telegram_chats[str(chat.id)]
    except KeyError:
        print('Unknown telegram chat {}'.format(chat))
        return

    user_id = USER_ID_FORMAT.format(chat.sender['id'])
    txn_id = quote('{}:{}'.format(chat.message['message_id'], chat.id))

    file_id = photo[-1]['file_id']
    uri, mime, length = await upload_tgfile_to_matrix(file_id, user_id)
    info = {'mimetype': mime, 'size': length, 'h': photo[-1]['height'], 'w': photo[-1]['width']}
    body = 'Image_{}{}'.format(int(time.time() * 1000), mimetypes.guess_extension(mime))

    if uri:
        j = await send_matrix_message(room_id, user_id, txn_id, body=body, url=uri, info=info, msgtype='m.image')
        if 'errcode' in j and j['errcode'] == 'M_FORBIDDEN':
            await register_join_matrix(chat, room_id, user_id)
            await send_matrix_message(room_id, user_id, txn_id, body=body, url=uri, info=info, msgtype='m.image')

@bot.command(r'(?s)(.*)')
async def aiotg_message(chat, match):
    try:
        room_id = telegram_chats[str(chat.id)]
    except KeyError:
        print('Unknown telegram chat {}'.format(chat))
        return

    user_id = USER_ID_FORMAT.format(chat.sender['id'])
    txn_id = quote('{}:{}'.format(chat.message['message_id'], chat.id))
    message = match.group(0)

    j = await send_matrix_message(room_id, user_id, txn_id, body=message, msgtype='m.text')
    if 'errcode' in j and j['errcode'] == 'M_FORBIDDEN':
        await asyncio.sleep(1)
        await register_join_matrix(chat, room_id, user_id)
        await asyncio.sleep(1)
        await send_matrix_message(room_id, user_id, txn_id, body=message, msgtype='m.text')

if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)

    loop = asyncio.get_event_loop()
    asyncio.ensure_future(bot.loop())

    app = web.Application(loop=loop)
    app.router.add_route('GET', '/rooms/{room_alias}', matrix_room)
    app.router.add_route('PUT', '/transactions/{transaction}', matrix_transaction)
    web.run_app(app, port=5000)

