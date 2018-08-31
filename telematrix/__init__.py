"""
Telematrix

App service for Matrix to bridge a room with a Telegram group.
"""
import asyncio
import html
import json
import logging
import mimetypes
from datetime import datetime
from time import time
from urllib.parse import unquote, quote, urlparse, parse_qs
from io import BytesIO
import re

from PIL import Image
from aiohttp import web, ClientSession
from aiotg import Bot
from bs4 import BeautifulSoup

import telematrix.database as db

# Read the configuration file
try:
    with open('config.json', 'r') as config_file:
        CONFIG = json.load(config_file)

        HS_TOKEN = CONFIG['tokens']['hs']
        AS_TOKEN = CONFIG['tokens']['as']
        TG_TOKEN = CONFIG['tokens']['telegram']

        try:
            GOOGLE_TOKEN = CONFIG['tokens']['google']
        except KeyError:
            GOOGLE_TOKEN = None

        MATRIX_HOST = CONFIG['hosts']['internal']
        MATRIX_HOST_EXT = CONFIG['hosts']['external']
        MATRIX_HOST_BARE = CONFIG['hosts']['bare']

        MATRIX_PREFIX = MATRIX_HOST + '_matrix/client/r0/'
        MATRIX_MEDIA_PREFIX = MATRIX_HOST + '_matrix/media/r0/'

        USER_ID_FORMAT = CONFIG['user_id_format']
        DATABASE_URL = CONFIG['db_url']
        HIDE_MEMBERSHIP_CHANGES = CONFIG['hide_membership_changes']

        AS_PORT = CONFIG['as_port'] if 'as_port' in CONFIG else 5000
except (OSError, IOError) as exception:
    print('Error opening config file:')
    print(exception)
    exit(1)

GOO_GL_URL = 'https://www.googleapis.com/urlshortener/v1/url'

TG_BOT = Bot(api_token=TG_TOKEN)
MATRIX_SESS = ClientSession()
SHORTEN_SESS = ClientSession()

MT = mimetypes.MimeTypes()


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
            tag.string = ('\n' + tag.text).replace('\n', '\n> ')[3:-3]
        if tag.name not in VALID_TAGS:
            tag.hidden = True
    return soup.renderContents().decode('utf-8')


def format_matrix_msg(form, content):
    """
    Formats a matrix message for sending to Telegram
    :param form: The format string of the message, where the first parameter
                 is the username and the second one the message.
    :param content: The content to be sent.
    :return: The formatted string.
    """
    if 'format' in content and content['format'] == 'org.matrix.custom.html':
        sanitized = re.sub("<a href=\\\"https://matrix.to/#/@telegram_([0-9]+):{}\\\">(.+?) \(Telegram\)</a>".format(MATRIX_HOST_BARE), "<a href=\"tg://user?id=\\1\">\\2</a>", content['formatted_body'])
        sanitized = sanitize_html(sanitized)
        return html.escape(form).format(sanitized), 'HTML'
    else:
        return form.format(html.escape(content['body'])), None


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
    if not GOOGLE_TOKEN:
        return url

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


def matrix_is_telegram(user_id):
    """
    Check if the user is a virtual telegram user or a real matrix user. Returns True if it is a fake
    user (telegram virtual user).
    :param user_id: The matrix id of the user.
    :return: True if a virtual telegram user.
    """
    username = user_id.split(':')[0][1:]
    return username.startswith('telegram_')


def get_username(user_id):
    return user_id.split(':')[0][1:]


async def matrix_transaction(request):
    """
    Handle a transaction sent by the homeserver.
    :param request: The request containing the transaction.
    :return: The response to send.
    """
    body = await request.json()
    events = body['events']
    for event in events:
        if 'age' in event and event['age'] > 600000:
            print('discarded event of age', event['age'])
            continue
        try:
            print('{}: <{}> {}'.format(event['room_id'], event['user_id'], event['type']))
        except KeyError:
            pass

        if event['type'] == 'm.room.aliases' and event['state_key'] == MATRIX_HOST_BARE:
            aliases = event['content']['aliases']

            links = db.session.query(db.ChatLink)\
                      .filter_by(matrix_room=event['room_id']).all()
            for link in links:
                db.session.delete(link)

            for alias in aliases:
                print(alias)
                if alias.split('_')[0] != '#telegram' \
                        or alias.split(':')[-1] != MATRIX_HOST_BARE:
                    continue

                tg_id = alias.split('_')[1].split(':')[0]
                link = db.ChatLink(event['room_id'], tg_id, True)
                db.session.add(link)
                db.session.commit()

            continue

        link = db.session.query(db.ChatLink)\
                 .filter_by(matrix_room=event['room_id']).first()
        if not link:
            print('{} isn\'t linked!'.format(event['room_id']))
            continue
        group = TG_BOT.group(link.tg_room)

        try:
            response = None

            if event['type'] == 'm.room.message':
                user_id = event['user_id']
                if matrix_is_telegram(user_id):
                    continue

                sender = db.session.query(db.MatrixUser)\
                           .filter_by(matrix_id=user_id).first()

                if not sender:
                    response = await matrix_get('client', 'profile/{}/displayname'
                                                          .format(user_id), None)
                    try:
                        displayname = response['displayname']
                    except KeyError:
                        displayname = get_username(user_id)
                    sender = db.MatrixUser(user_id, displayname)
                    db.session.add(sender)
                else:
                    displayname = sender.name or get_username(user_id)
                content = event['content']

                if 'msgtype' not in content:
                    continue

                if content['msgtype'] == 'm.text':
                    msg, mode = format_matrix_msg('{}', content)
                    response = await group.send_text("<b>{}:</b> {}".format(displayname, msg),
                                                     parse_mode='HTML')
                elif content['msgtype'] == 'm.notice':
                    msg, mode = format_matrix_msg('{}', content)
                    response = await group.send_text("[{}] {}".format(displayname, msg),
                                                     parse_mode=mode)
                elif content['msgtype'] == 'm.emote':
                    msg, mode = format_matrix_msg('{}', content)
                    response = await group.send_text("* {} {}".format(displayname, msg),
                                                     parse_mode=mode)
                elif content['msgtype'] in ['m.image', 'm.audio', 'm.video', 'm.file']:
                    try:
                        url = urlparse(content['url'])

                        # Append the correct extension if it's missing or wrong
                        try:
                            exts = MT.types_map_inv[1][content['info']['mimetype']]
                            if not content['body'].endswith(tuple(exts)):
                                content['body'] += '.' + exts[0]
                        except KeyError:
                            pass

                        # Download the file
                        await download_matrix_file(url, content['body'])
                        with open('/tmp/{}'.format(content['body']), 'rb') as file:
                            # Create the URL and shorten it
                            url_str = MATRIX_HOST_EXT + \
                                      '_matrix/media/r0/download/{}{}' \
                                      .format(url.netloc, quote(url.path))
                            url_str = await shorten_url(url_str)

                            if content['msgtype'] == 'm.image':
                                if content['info']['mimetype'] == 'image/gif':
                                    # Send gif as a video, so telegram can display it animated
                                    caption = '{} sent a gif'.format(displayname)
                                    await group.send_chat_action('upload_video')
                                    response = await group.send_video(file, caption=caption)
                                else:
                                    caption = '{} sent an image'.format(displayname)
                                    await group.send_chat_action('upload_photo')
                                    response = await group.send_photo(file, caption=caption)
                            elif content['msgtype'] == 'm.video':
                                caption = '{} sent a video'.format(displayname)
                                await group.send_chat_action('upload_video')
                                response = await group.send_video(file, caption=caption)
                            elif content['msgtype'] == 'm.audio':
                                caption = '{} sent an audio file'.format(displayname)
                                await group.send_chat_action('upload_audio')
                                response = await group.send_audio(file, caption=caption)
                            elif content['msgtype'] == 'm.file':
                                caption = '{} sent a file'.format(displayname)
                                await group.send_chat_action('upload_document')
                                response = await group.send_document(file, caption=caption)
                    except:
                        pass
                else:
                    print('Unsupported message type {}'.format(content['msgtype']))
                    print(json.dumps(content, indent=4))

            elif event['type'] == 'm.sticker':
                user_id = event['user_id']
                if matrix_is_telegram(user_id):
                    continue

                sender = db.session.query(db.MatrixUser) \
                    .filter_by(matrix_id=user_id).first()

                if not sender:
                    response = await matrix_get('client', 'profile/{}/displayname'
                                                .format(user_id), None)
                    try:
                        displayname = response['displayname']
                    except KeyError:
                        displayname = get_username(user_id)
                    sender = db.MatrixUser(user_id, displayname)
                    db.session.add(sender)
                else:
                    displayname = sender.name or get_username(user_id)
                content = event['content']

                try:
                    url = urlparse(content['url'])
                    await download_matrix_file(url, content['body'])

                    png_image = Image.open('/tmp/{}'.format(content['body']))
                    png_image.save('/tmp/{}.webp'.format(content['body']), 'WEBP')
                    with open('/tmp/{}.webp'.format(content['body']), 'rb') as file:
                        response = await group.send_document(file)

                except:
                    pass

            elif event['type'] == 'm.room.member':
                if HIDE_MEMBERSHIP_CHANGES:  # Hide everything, could be improved to be
                    # more specific
                    continue
                if matrix_is_telegram(event['state_key']):
                    continue

                user_id = event['state_key']
                content = event['content']

                sender = db.session.query(db.MatrixUser)\
                           .filter_by(matrix_id=user_id).first()
                if sender:
                    displayname = sender.name
                else:
                    displayname = get_username(user_id)

                if content['membership'] == 'join':
                    oldname = sender.name if sender else get_username(user_id)
                    try:
                        displayname = content['displayname'] or get_username(user_id)
                    except KeyError:
                        displayname = get_username(user_id)

                    if not sender:
                        sender = db.MatrixUser(user_id, displayname)
                    else:
                        sender.name = displayname
                    db.session.add(sender)

                    msg = None
                    if 'unsigned' in event and 'prev_content' in event['unsigned']:
                        prev = event['unsigned']['prev_content']
                        if prev['membership'] == 'join':
                            if 'displayname' in prev and prev['displayname']:
                                oldname = prev['displayname']

                            msg = '> {} changed their display name to {}'\
                                  .format(oldname, displayname)
                    else:
                        msg = '> {} has joined the room'.format(displayname)

                    if msg:
                        response = await group.send_text(msg)
                elif content['membership'] == 'leave':
                    msg = '< {} has left the room'.format(displayname)
                    response = await group.send_text(msg)
                elif content['membership'] == 'ban':
                    msg = '<! {} was banned from the room'.format(displayname)
                    response = await group.send_text(msg)

            if response:
                message = db.Message(
                    response['result']['chat']['id'],
                    response['result']['message_id'],
                    event['room_id'],
                    event['event_id'],
                    displayname)
                db.session.add(message)

        except RuntimeError as e:
            print('Got a runtime error:', e)
            print('Group:', group)

    db.session.commit()
    return create_response(200, {})


async def _matrix_request(method_fun, category, path, user_id, data=None,
                          content_type=None):
    # pylint: disable=too-many-arguments
    # Due to this being a helper function, the argument count acceptable
    if content_type is None:
        content_type = 'application/octet-stream'
    if data is not None:
        if isinstance(data, dict):
            data = json.dumps(data)
            content_type = 'application/json; charset=utf-8'

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

    # Look up the chat in the database
    link = db.session.query(db.ChatLink).filter_by(tg_room=chat).first()
    if link:
        await matrix_post('client', 'createRoom', None,
                          {'room_alias_name': localpart[1:]})
        return create_response(200, {})
    else:
        return create_response(404, {'errcode':
                                     'NL.SIJMENSCHOON.TELEMATRIX_NOT_FOUND'})


def send_matrix_message(room_id, user_id, txn_id, **kwargs):
    url = 'rooms/{}/send/m.room.message/{}'.format(room_id, txn_id)
    return matrix_put('client', url, user_id, kwargs)


async def upload_tgfile_to_matrix(file_id, user_id, mime='image/jpeg', convert_to=None):
    file_path = (await TG_BOT.get_file(file_id))['file_path']
    request = await TG_BOT.download_file(file_path)
    data = await request.read()

    if convert_to:
        image = Image.open(BytesIO(data))
        png_image = BytesIO(None)
        image.save(png_image, convert_to)

        j = await matrix_post('media', 'upload', user_id, png_image.getvalue(), mime)
        length = len(png_image.getvalue())
    else:
        j = await matrix_post('media', 'upload', user_id, data, mime)
        length = len(data)

    if 'content_uri' in j:
        return j['content_uri'], length
    else:
        return None, 0


async def upload_file_to_matrix(file_id, user_id, mime):
    """
    Upload to the matrix homeserver all kinds of files based on mime informations.
    :param file_id: Telegram file id
    :param user_id: Matrix user id
    :param mime: mime type of the file
    :return: Tuple (JSON response, data length) if success, None else
    """
    file_path = (await TG_BOT.get_file(file_id))['file_path']
    request = await TG_BOT.download_file(file_path)
    data = await request.read()

    j = await matrix_post('media', 'upload', user_id, data, mime)
    length = len(data)

    if 'content_uri' in j:
        return j['content_uri'], length
    else:
        return None, 0


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
        pp_uri, _ = await upload_tgfile_to_matrix(pp_file_id, user_id)
        if pp_uri:
            await matrix_put('client', 'profile/{}/avatar_url'.format(user_id),
                             user_id, {'avatar_url': pp_uri})
    except IndexError:
        pass

    await matrix_put('client', 'profile/{}/displayname'.format(user_id),
                     user_id, {'displayname': name})
    j = await matrix_post('client', 'join/{}'.format(room_id), user_id, {})
    if 'errcode' in j and j['errcode'] == 'M_FORBIDDEN':
        print("Error with <{}> joining room <{}>. This is likely because guests are not allowed to join the room."
              .format(user_id, room_id))


async def update_matrix_displayname_avatar(tg_user):
    name = tg_user['first_name']
    if 'last_name' in tg_user:
        name += ' ' + tg_user['last_name']
    name += ' (Telegram)'
    user_id = USER_ID_FORMAT.format(tg_user['id'])
    
    db_user = db.session.query(db.TgUser).filter_by(tg_id=tg_user['id']).first()

    profile_photos = await TG_BOT.get_user_profile_photos(tg_user['id'])
    pp_file_id = None
    try:
        pp_file_id = profile_photos['result']['photos'][0][-1]['file_id']
    except:
        pp_file_id = None

    if db_user:
        if db_user.name != name:
            await matrix_put('client', 'profile/{}/displayname'.format(user_id), user_id, {'displayname': name})
            db_user.name = name
        if db_user.profile_pic_id != pp_file_id:
            if pp_file_id:
                pp_uri, _ = await upload_tgfile_to_matrix(pp_file_id, user_id)
                await matrix_put('client', 'profile/{}/avatar_url'.format(user_id), user_id, {'avatar_url':pp_uri})
            else:
                await matrix_put('client', 'profile/{}/avatar_url'.format(user_id), user_id, {'avatar_url':None})
            db_user.profile_pic_id = pp_file_id
    else:
        db_user = db.TgUser(tg_user['id'], name, pp_file_id)
        await matrix_put('client', 'profile/{}/displayname'.format(user_id), user_id, {'displayname': name})
        if pp_file_id:
            pp_uri, _ = await upload_tgfile_to_matrix(pp_file_id, user_id)
            await matrix_put('client', 'profile/{}/avatar_url'.format(user_id), user_id, {'avatar_url':pp_uri})
        else:
            await matrix_put('client', 'profile/{}/avatar_url'.format(user_id), user_id, {'avatar_url':None})
        db.session.add(db_user)
    db.session.commit()


def create_file_name(obj_type, mime):
    try:
        ext = MT.types_map_inv[1][mime][0]
    except KeyError:
        try:
            ext = MT.types_map_inv[0][mime][0]
        except KeyError:
            ext = ''
    name = '{}_{}{}'.format(obj_type, int(time() * 1000), ext)
    return name


async def send_file_to_matrix(chat, room_id, user_id, txn_id, body, uri, info, msgtype):
    j = await send_matrix_message(room_id, user_id, txn_id, body=body,
                        url=uri, info=info, msgtype=msgtype)

    if 'errcode' in j and j['errcode'] == 'M_FORBIDDEN':
        await register_join_matrix(chat, room_id, user_id)
        await send_matrix_message(room_id, user_id, txn_id + 'join',
                                  body=body, url=uri, info=info, msgtype=msgtype)

    if 'caption' in chat.message:
        await send_matrix_message(room_id, user_id, txn_id + 'caption',
                                  body=chat.message['caption'], msgtype='m.text')

    if 'event_id' in j:
        name = chat.sender['first_name']
        if 'last_name' in chat.sender:
            name += " " + chat.sender['last_name']
        name += " (Telegram)"
        message = db.Message(
            chat.message['chat']['id'],
            chat.message['message_id'],
            room_id,
            j['event_id'],
            name)
        db.session.add(message)
        db.session.commit()


@TG_BOT.handle('sticker')
async def aiotg_sticker(chat, sticker):
    link = db.session.query(db.ChatLink).filter_by(tg_room=chat.id).first()
    if not link:
        print('Unknown telegram chat {}: {}'.format(chat, chat.id))
        return

    await update_matrix_displayname_avatar(chat.sender)

    room_id = link.matrix_room
    user_id = USER_ID_FORMAT.format(chat.sender['id'])
    txn_id = quote('{}{}'.format(chat.message['message_id'], chat.id))

    file_id = sticker['file_id']
    uri, length = await upload_tgfile_to_matrix(file_id, user_id, 'image/png', 'PNG')

    info = {'mimetype': 'image/png', 'size': length, 'h': sticker['height'],
            'w': sticker['width']}
    body = 'Sticker_{}.png'.format(int(time() * 1000))

    if uri:
        await send_file_to_matrix(chat, room_id, user_id, txn_id, body, uri, info, 'm.image')


@TG_BOT.handle('photo')
async def aiotg_photo(chat, photo):
    link = db.session.query(db.ChatLink).filter_by(tg_room=chat.id).first()
    if not link:
        print('Unknown telegram chat {}: {}'.format(chat, chat.id))
        return

    await update_matrix_displayname_avatar(chat.sender);
    room_id = link.matrix_room
    user_id = USER_ID_FORMAT.format(chat.sender['id'])
    txn_id = quote('{}{}'.format(chat.message['message_id'], chat.id))

    file_id = photo[-1]['file_id']

    uri, length = await upload_tgfile_to_matrix(file_id, user_id)
    info = {'mimetype': 'image/jpeg', 'size': length, 'h': photo[-1]['height'],
            'w': photo[-1]['width']}
    body = 'Image_{}.jpg'.format(int(time() * 1000))

    if uri:
        await send_file_to_matrix(chat, room_id, user_id, txn_id, body, uri, info, 'm.image')


@TG_BOT.handle('audio')
async def aiotg_audio(chat, audio):
    link = db.session.query(db.ChatLink).filter_by(tg_room=chat.id).first()
    if not link:
        print('Unknown telegram chat {}: {}'.format(chat, chat.id))
        return

    await update_matrix_displayname_avatar(chat.sender);
    room_id = link.matrix_room
    user_id = USER_ID_FORMAT.format(chat.sender['id'])
    txn_id = quote('{}{}'.format(chat.message['message_id'], chat.id))

    file_id = audio['file_id']
    try:
        mime = audio['mime_type']
    except KeyError:
        mime = 'audio/mp3'
    uri, length = await upload_file_to_matrix(file_id, user_id, mime)
    info = {'mimetype': mime, 'size': length}
    body = create_file_name('Audio', mime)

    if uri:
        await send_file_to_matrix(chat, room_id, user_id, txn_id, body, uri, info, 'm.audio')


@TG_BOT.handle('document')
async def aiotg_document(chat, document):
    link = db.session.query(db.ChatLink).filter_by(tg_room=chat.id).first()
    if not link:
        print('Unknown telegram chat {}: {}'.format(chat, chat.id))
        return

    await update_matrix_displayname_avatar(chat.sender);
    room_id = link.matrix_room
    user_id = USER_ID_FORMAT.format(chat.sender['id'])
    txn_id = quote('{}{}'.format(chat.message['message_id'], chat.id))

    file_id = document['file_id']
    try:
        mime = document['mime_type']
    except KeyError:
        mime = ''
    uri, length = await upload_file_to_matrix(file_id, user_id, mime)
    info = {'mimetype': mime, 'size': length}

    if uri:
        # We check if the document can be sent in a better way (for example a photo or a gif)
        # For gif, that's still not perfect : it's sent as a video to matrix instead of a real
        # gif image
        if 'image' in mime:
            msgtype = 'm.image'
            body = create_file_name('Image', mime)
        elif 'video' in mime:
            msgtype = 'm.video'
            body = create_file_name('Video', mime)
        elif 'audio' in mime:
            msgtype = 'm.audio'
            body = create_file_name('Audio', mime)
        else:
            msgtype = 'm.file'
            body = create_file_name('File', mime)
        await send_file_to_matrix(chat, room_id, user_id, txn_id, body, uri, info, msgtype)


# This doesn't catch video from telegram, I don't know why
# The handler is never called
@TG_BOT.handle('video')
async def aiotg_video(chat, video):
    link = db.session.query(db.ChatLink).filter_by(tg_room=chat.id).first()
    if not link:
        print('Unknown telegram chat {}: {}'.format(chat, chat.id))
        return

    await update_matrix_displayname_avatar(chat.sender);
    room_id = link.matrix_room
    user_id = USER_ID_FORMAT.format(chat.sender['id'])
    txn_id = quote('{}{}'.format(chat.message['message_id'], chat.id))

    file_id = video['file_id']
    try:
        mime = video['mime_type']
    except KeyError:
        mime = 'video/mp4'
    uri, length = await upload_file_to_matrix(file_id, user_id, mime)
    info = {'mimetype': mime, 'size': length, 'h': video['height'],
            'w': video['width']}
    body = create_file_name('Video', mime)

    if uri:
        await send_file_to_matrix(chat, room_id, user_id, txn_id, body, uri, info, 'm.video')


@TG_BOT.command(r'/alias')
async def aiotg_alias(chat, match):
    await chat.reply('The Matrix alias for this chat is #telegram_{}:{}'
                     .format(chat.id, MATRIX_HOST_BARE))


@TG_BOT.command(r'(?s)(.*)')
async def aiotg_message(chat, match):
    link = db.session.query(db.ChatLink).filter_by(tg_room=chat.id).first()
    if link:
        room_id = link.matrix_room
    else:
        print('Unknown telegram chat {}: {}'.format(chat, chat.id))
        return

    await update_matrix_displayname_avatar(chat.sender);
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

        quoted_msg = '\n'.join(['>{}'.format(x) for x in message.split('\n')])
        quoted_msg = 'Forwarded from {}:\n{}' \
                     .format(msg_from, quoted_msg)

        quoted_html = '<blockquote>{}</blockquote>' \
                      .format(html.escape(message).replace('\n', '<br />'))
        quoted_html = '<i>Forwarded from {}:</i>\n{}' \
                      .format(html.escape(msg_from), quoted_html)
        j = await send_matrix_message(room_id, user_id, txn_id,
                                      body=quoted_msg,
                                      formatted_body=quoted_html,
                                      format='org.matrix.custom.html',
                                      msgtype='m.text')

    elif 'reply_to_message' in chat.message:
        re_msg = chat.message['reply_to_message']
        if not 'text' in re_msg and not 'photo' in re_msg and not 'sticker' in re_msg:
            return
        if 'last_name' in re_msg['from']:
            msg_from = '{} {} (Telegram)'.format(re_msg['from']['first_name'],
                                                 re_msg['from']['last_name'])
        else:
            msg_from = '{} (Telegram)'.format(re_msg['from']['first_name'])
        date = datetime.fromtimestamp(re_msg['date']).strftime('%Y-%m-%d %H:%M:%S')

        reply_mx_id = db.session.query(db.Message)\
            .filter_by(tg_group_id=chat.message['chat']['id'],
                       tg_message_id=chat.message['reply_to_message']['message_id']).first()

        html_message = html.escape(message).replace('\n', '<br />')
        if 'text' in re_msg:
            quoted_msg = '\n'.join(['>{}'.format(x)
                                    for x in re_msg['text'].split('\n')])
            quoted_html = '<blockquote>{}</blockquote>' \
                          .format(html.escape(re_msg['text'])
                                  .replace('\n', '<br />'))
        else:
            quoted_msg = ''
            quoted_html = ''

        if reply_mx_id:
            quoted_msg = 'Reply to {}:\n{}\n\n{}' \
                         .format(reply_mx_id.displayname, quoted_msg, message)
            quoted_html = '<i><a href="https://matrix.to/#/{}/{}">Reply to {}</a>:</i><br />{}<p>{}</p>' \
                          .format(html.escape(room_id),
                                  html.escape(reply_mx_id.matrix_event_id),
                                  html.escape(reply_mx_id.displayname),
                                  quoted_html, html_message)
        else:
            quoted_msg = 'Reply to {}:\n{}\n\n{}' \
                         .format(msg_from, quoted_msg, message)
            quoted_html = '<i>Reply to {}:</i><br />{}<p>{}</p>' \
                          .format(html.escape(msg_from),
                                  quoted_html, html_message)

        j = await send_matrix_message(room_id, user_id, txn_id,
                                      body=quoted_msg,
                                      formatted_body=quoted_html,
                                      format='org.matrix.custom.html',
                                      msgtype='m.text')
    else:
        j = await send_matrix_message(room_id, user_id, txn_id, body=message,
                                      msgtype='m.text')

    if 'errcode' in j and j['errcode'] == 'M_FORBIDDEN':
        await register_join_matrix(chat, room_id, user_id)
        await asyncio.sleep(0.5)
        j = await send_matrix_message(room_id, user_id, txn_id + 'join',
                                      body=message, msgtype='m.text')
    elif 'event_id' in j:
        name = chat.sender['first_name']
        if 'last_name' in chat.sender:
            name += " " + chat.sender['last_name']
        name += " (Telegram)"
        message = db.Message(
                chat.message['chat']['id'],
                chat.message['message_id'],
                room_id,
                j['event_id'],
                name)
        db.session.add(message)
        db.session.commit()


def main():
    """
    Main function to get the entire ball rolling.
    """
    logging.basicConfig(level=logging.WARNING)
    db.initialize(DATABASE_URL)

    loop = asyncio.get_event_loop()
    asyncio.ensure_future(TG_BOT.loop())

    app = web.Application(loop=loop)
    app.router.add_route('GET', '/rooms/{room_alias}', matrix_room)
    app.router.add_route('PUT', '/transactions/{transaction}',
                         matrix_transaction)
    web.run_app(app, port=AS_PORT)


if __name__ == "__main__":
    main()
