"""
Telematrix

App service for Matrix to bridge a room with a Telegram group.
"""
import asyncio
import html
import json
import logging
import mimetypes
import sys
from datetime import datetime
from time import time
from urllib.parse import unquote, quote, urlparse, parse_qs

from aiohttp import web, ClientSession
from aiotg import Bot
from bs4 import BeautifulSoup

# Read the configuration file
try:
    with open('config.json', 'r') as config_file:
        CONFIG = json.load(config_file)

        HS_TOKEN = CONFIG['tokens']['hs']
        AS_TOKEN = CONFIG['tokens']['as']
        TG_TOKEN = CONFIG['tokens']['telegram']
        GOOGLE_TOKEN = CONFIG['tokens']['google']

        MATRIX_HOST = CONFIG['hosts']['internal']
        MATRIX_HOST_EXT = CONFIG['hosts']['external']

        MATRIX_PREFIX = MATRIX_HOST + '_matrix/client/r0/'
        MATRIX_MEDIA_PREFIX = MATRIX_HOST + '_matrix/media/r0/'

        USER_ID_FORMAT = CONFIG['user_id_format']

        TELEGRAM_CHATS = CONFIG['chats']
        MATRIX_ROOMS = {v: k for k, v in TELEGRAM_CHATS.items()}
except (OSError, IOError) as exception:
    print('Error opening config file:')
    print(exception)
    exit(1)

GOO_GL_URL = 'https://www.googleapis.com/urlshortener/v1/url'

TG_BOT = Bot(api_token=TG_TOKEN)
MATRIX_SESS = ClientSession()
SHORTEN_SESS = ClientSession()


def create_response(code, obj):
    """
    Create an HTTP response with a JSON body.
    :param code: The status code of the response.
    :param obj: The object to serialize and include in the response.
    :return: A web.Response.
    """
    return web.Response(text=json.dumps(obj), status=code,
                        content_type='application/json', charset='utf-8')


VALID_TAGS = ['b', 'strong', 'i', 'em', 'a', 'pre']


def sanitize_html(string):
    """
    Sanitize an HTML string for the Telegram bot API.
    :param string: The HTML string to sanitized.
    :return: The sanitized HTML string.
    """
    string = string.replace('<br>', '\n').replace('<br/>', '\n') \
                   .replace('<br />', '\n')
    soup = BeautifulSoup(string, 'html.parser')
    for tag in soup.find_all(True):
        if tag.name == 'blockquote':
            tag.string = ('\n' + tag.text).replace('\n', '\n> ').rstrip('\n>')
        if tag.name not in VALID_TAGS:
            tag.hidden = True
    return soup.renderContents().decode('utf-8')


def format_matrix_msg(form, username, content):
    """
    Formats a matrix message for sending to Telegram
    :param form: The format string of the message, where the first parameter
                 is the username and the second one the message.
    :param username: The username of the user.
    :param content: The content to be sent.
    :return: The formatted string.
    """
    if 'format' in content and content['format'] == 'org.matrix.custom.html':
        sanitized = sanitize_html(content['formatted_body'])
        return html.escape(form).format(username, sanitized), 'HTML'
    else:
        return form.format(username, content['body']), None


async def download_matrix_file(url, filename):
    """
    Download a file from an MXC URL to /tmp/{filename}
    :param url: The MXC URL to download from.
    :param filename: The filename in /tmp/ to download into.
    """
    m_url = MATRIX_MEDIA_PREFIX + 'download/{}{}'.format(url.netloc, url.path)
    async with MATRIX_SESS.get(m_url) as response:
        data = await response.read()
    with open('/tmp/{}'.format(filename), 'wb') as file:
        file.write(data)


async def shorten_url(url):
    """
    Shorten an URL using goo.gl. Returns the original URL if it fails.
    :param url: The URL to shorten.
    :return: The shortened URL.
    """
    headers = {'Content-Type': 'application/json'}
    async with SHORTEN_SESS.post(GOO_GL_URL, params={'key': GOOGLE_TOKEN},
                                 data=json.dumps({'longUrl': url}), 
                                 headers=headers) \
            as response:
        obj = await response.json()

    if 'id' in obj:
        return obj['id']
    else:
        return url


async def matrix_transaction(request):
    """
    Handle a transaction sent by the homeserver.
    :param request: The request containing the transaction.
    :return: The response to send.
    """
    body = await request.json()
    events = body['events']
    for event in events:
        if event['room_id'] not in MATRIX_ROOMS:
            print('{} not in matrix_rooms!'.format(event['room_id']))
        elif event['type'] == 'm.room.message':
            group = TG_BOT.group(MATRIX_ROOMS[event['room_id']])

            username = event['user_id'].split(':')[0][1:]
            if username.startswith('telegram_'):
                return create_response(200, {})

            content = event['content']
            if content['msgtype'] == 'm.text':
                msg, mode = format_matrix_msg('<{}> {}', username, content)
                await group.send_text(msg, parse_mode=mode)
            elif content['msgtype'] == 'm.notice':
                msg, mode = format_matrix_msg('[{}] {}', username, content)
                await group.send_text(msg, parse_mode=mode)
            elif content['msgtype'] == 'm.emote':
                msg, mode = format_matrix_msg('* {} {}', username, content)
                await group.send_text(msg, parse_mode=mode)
            elif content['msgtype'] == 'm.image':
                url = urlparse(content['url'])
                await download_matrix_file(url, content['body'])
                with open('/tmp/{}'.format(content['body']), 'rb') as img_file:
                    url_str = MATRIX_HOST_EXT + \
                              '_matrix/media/r0/download/{}{}' \
                              .format(url.netloc, quote(url.path))
                    url_str = await shorten_url(url_str)

                    caption = '<{}> {} ({})'.format(username, content['body'],
                                                    url_str)
                    await group.send_photo(img_file, caption=caption)
            else:
                print('Unsupported message type {}'.format(content['msgtype']))
                print(json.dumps(content, indent=4))

    return create_response(200, {})


async def _matrix_request(method_fun, category, path, user_id, data=None,
                          content_type=None):
    # pylint: disable=too-many-arguments
    # Due to this being a helper function, the argument count acceptable
    if data is not None:
        if isinstance(data, dict):
            data = json.dumps(data)
            content_type = 'application/json; charset=utf-8'
        elif content_type is None:
            content_type = 'application/octet-stream'

    params = {'access_token': AS_TOKEN}
    if user_id is not None:
        params['user_id'] = user_id

    async with method_fun('{}_matrix/{}/r0/{}'
                          .format(MATRIX_HOST, quote(category), quote(path)),
                          params=params, data=data,
                          headers={'Content-Type': content_type}) as response:
        if response.headers['Content-Type'].split(';')[0] \
                == 'application/json':
            return await response.json()
        else:
            return await response.read()


def matrix_post(category, path, user_id, data, content_type=None):
    return _matrix_request(MATRIX_SESS.post, category, path, user_id, data,
                           content_type)


def matrix_put(category, path, user_id, data, content_type=None):
    return _matrix_request(MATRIX_SESS.put, category, path, user_id, data,
                           content_type)


def matrix_get(category, path, user_id):
    return _matrix_request(MATRIX_SESS.get, category, path, user_id)


def matrix_delete(category, path, user_id):
    return _matrix_request(MATRIX_SESS.delete, category, path, user_id)


async def matrix_room(request):
    room_alias = request.match_info['room_alias']
    args = parse_qs(urlparse(request.path_qs).query)
    print('Checking for {} | {}'.format(unquote(room_alias),
                                        args['access_token'][0]))

    try:
        if args['access_token'][0] != HS_TOKEN:
            return create_response(403, {'errcode': 'M_FORBIDDEN'})
    except KeyError:
        return create_response(401,
                               {'errcode':
                                'NL.SIJMENSCHOON.TELEMATRIX_UNAUTHORIZED'})

    localpart = room_alias.split(':')[0]
    chat = '_'.join(localpart.split('_')[1:])

    if chat in TELEGRAM_CHATS:
        await matrix_post('client', 'createRoom', None,
                          {'room_alias_name': localpart[1:]})
        return create_response(200, {})
    else:
        return create_response(404, {'errcode':
                                     'NL.SIJMENSCHOON.TELEMATRIX_NOT_FOUND'})


def send_matrix_message(room_id, user_id, txn_id, **kwargs):
    return matrix_put('client', 'rooms/{}/send/m.room.message/{}'
                      .format(room_id, txn_id), user_id, kwargs)


async def upload_tgfile_to_matrix(file_id, user_id):
    file_path = (await TG_BOT.get_file(file_id))['file_path']
    request = await TG_BOT.download_file(file_path)
    mimetype = request.headers['Content-Type']

    data = await request.read()
    j = await matrix_post('media', 'upload', user_id, data, mimetype)

    if 'content_uri' in j:
        return j['content_uri'], mimetype, len(data)
    else:
        return None, None, 0


async def register_join_matrix(chat, room_id, user_id):
    name = chat.sender['first_name']
    if 'last_name' in chat.sender:
        name += ' ' + chat.sender['last_name']
    name += ' (Telegram)'
    user = user_id.split(':')[0][1:]

    await matrix_post('client', 'register', None,
                      {'type': 'm.login.application_service', 'user': user})
    profile_photos = await TG_BOT.get_user_profile_photos(chat.sender['id'])
    try:
        pp_file_id = profile_photos['result']['photos'][0][-1]['file_id']
        pp_uri, _, _ = await upload_tgfile_to_matrix(pp_file_id, user_id)
        if pp_uri:
            await matrix_put('client', 'profile/{}/avatar_url'.format(user_id),
                             user_id, {'avatar_url': pp_uri})
    except IndexError:
        pass

    await matrix_put('client', 'profile/{}/displayname'.format(user_id),
                     user_id, {'displayname': name})
    await matrix_post('client', 'join/{}'.format(room_id), user_id, {})


@TG_BOT.handle('photo')
async def aiotg_photo(chat, photo):
    try:
        room_id = TELEGRAM_CHATS[str(chat.id)]
    except KeyError:
        print('Unknown telegram chat {}'.format(chat))
        return

    user_id = USER_ID_FORMAT.format(chat.sender['id'])
    txn_id = quote('{}:{}'.format(chat.message['message_id'], chat.id))

    file_id = photo[-1]['file_id']
    uri, mime, length = await upload_tgfile_to_matrix(file_id, user_id)
    info = {'mimetype': mime, 'size': length, 'h': photo[-1]['height'],
            'w': photo[-1]['width']}
    body = 'Image_{}{}'.format(int(time() * 1000),
                               mimetypes.guess_extension(mime))

    if uri:
        j = await send_matrix_message(room_id, user_id, txn_id, body=body,
                                      url=uri, info=info, msgtype='m.image')
        if 'errcode' in j and j['errcode'] == 'M_FORBIDDEN':
            await register_join_matrix(chat, room_id, user_id)
            await send_matrix_message(room_id, user_id, txn_id, body=body,
                                      url=uri, info=info, msgtype='m.image')


@TG_BOT.command(r'(?s)(.*)')
async def aiotg_message(chat, match):
    try:
        room_id = TELEGRAM_CHATS[str(chat.id)]
    except KeyError:
        print('Unknown telegram chat {}: {}'.format(chat, chat.id))
        return

    user_id = USER_ID_FORMAT.format(chat.sender['id'])
    txn_id = quote('{}:{}'.format(chat.message['message_id'], chat.id))

    message = match.group(0)

    if 'forward_from' in chat.message:
        fw_from = chat.message['forward_from']
        if 'last_name' in fw_from:
            msg_from = '{} {} (Telegram)'.format(fw_from['first_name'],
                                                 fw_from['last_name'])
        else:
            msg_from = '{} (Telegram)'.format(fw_from['first_name'])
        date = datetime.fromtimestamp(fw_from['date'])

        quoted_msg = '\n'.join(['>{}'.format(x) for x in message.split('\n')])
        quoted_msg = 'Forwarded from {}, who {} said:\n{}' \
                     .format(msg_from, date, quoted_msg)

        quoted_html = '<blockquote>{}</blockquote>' \
                      .format(html.escape(message).replace('\n', '<br />'))
        quoted_html = '<i>Forwarded from {}, who {} said:</i>\n{}' \
                      .format(html.escape(msg_from), html.escape(str(date)),
                              quoted_html)
        j = await send_matrix_message(room_id, user_id, txn_id,
                                      body=quoted_msg,
                                      formatted_body=quoted_html,
                                      format='org.matrix.custom.html',
                                      msgtype='m.text')

    elif 'reply_to_message' in chat.message:
        re_msg = chat.message['reply_to_message']
        if 'last_name' in re_msg['from']:
            msg_from = '{} {} (Telegram)'.format(re_msg['from']['first_name'],
                                                 re_msg['from']['last_name'])
        else:
            msg_from = '{} (Telegram)'.format(re_msg['from']['first_name'])
        date = datetime.fromtimestamp(re_msg['date']) \
               .strftime('on %Y-%m-%d at %H:%M:%S')

        quoted_msg = '\n'.join(['>{}'.format(x)
                                for x in re_msg['text'].split('\n')])
        quoted_msg = 'Reply to {}, who {} said:\n{}\n\n{}' \
                     .format(msg_from, date, quoted_msg, message)

        quoted_html = '<blockquote>{}</blockquote>' \
                      .format(html.escape(re_msg['text'])
                              .replace('\n', '<br />'))
        quoted_html = '<i>Reply to {}, who {} said:</i><br />{}<p>{}</p>' \
                      .format(html.escape(msg_from), html.escape(str(date)),
                              quoted_html,
                              html.escape(message).replace('\n', '<br />'))

        j = await send_matrix_message(room_id, user_id, txn_id,
                                      body=quoted_msg,
                                      formatted_body=quoted_html,
                                      format='org.matrix.custom.html',
                                      msgtype='m.text')
    else:
        j = await send_matrix_message(room_id, user_id, txn_id, body=message,
                                      msgtype='m.text')

    if 'errcode' in j and j['errcode'] == 'M_FORBIDDEN':
        await asyncio.sleep(1)
        await register_join_matrix(chat, room_id, user_id)
        await asyncio.sleep(1)
        await send_matrix_message(room_id, user_id, txn_id, body=message,
                                  msgtype='m.text')


def main():
    """
    Main function to get the entire ball rolling.
    """
    logging.basicConfig(level=logging.WARNING)

    loop = asyncio.get_event_loop()
    asyncio.ensure_future(TG_BOT.loop())

    app = web.Application(loop=loop)
    app.router.add_route('GET', '/rooms/{room_alias}', matrix_room)
    app.router.add_route('PUT', '/transactions/{transaction}',
                         matrix_transaction)
    web.run_app(app, port=5000)


if __name__ == "__main__":
    main()
