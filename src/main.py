#!/bin/python3

import sys, os
from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeVideo, DocumentAttributeAudio
from telethon.sessions import StringSession, SQLiteSession
import traceback
import asyncio
import logging
import logaugment
import youtube_dl
from aiohttp import web, ClientSession
from urlextract import URLExtract
import re
import av_utils
import av_source
import inspect
import mimetypes
from aiogram import Bot
from requests import HTTPError


def get_client_session():
    if 'CLIENT_SESSION' in os.environ:
        return os.environ['CLIENT_SESSION']

    try:
        from cloudant import cloudant
        from cloudant.adapters import Replay429Adapter
    except:
        raise Exception('Couldn\'t find client session nor in os.environ or cloudant db')

    with cloudant(os.environ['CLOUDANT_USERNAME'],
                  os.environ['CLOUDANT_PASSWORD'],
                  url=os.environ['CLOUDANT_URL'],
                  adapter=Replay429Adapter(retries=10),
                  connect=True) as client:
        db = client['ytbdownbot']
        instance_id = '0'
        # in case of multi instance architecture
        if 'INSTANCE_INDEX' in os.environ:
            instance_id = os.environ['INSTANCE_INDEX']
        return db['session'+instance_id]['session']


def new_logger(user_id, msg_id):
    logger = logging.Logger('')
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter("%(levelname)s <%(id)s> [%(msgid)s]: %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logaugment.set(logger, id=str(user_id), msgid=str(msg_id))

    return logger


async def on_message(request):
    try:
        req_data = await request.json()
        message = req_data['message']

        if message['from']['id'] == BOT_AGENT_CHAT_ID:
            await share_content_with_user(message)
            return web.Response(status=200)

        asyncio.get_event_loop().create_task(_on_message_task(message))
    except Exception as e:
        logging.exception(e)

    return web.Response(status=200)


# share uploaded by client api file to user
async def share_content_with_user(message):
    _user_id, _reply_msg_id = message['caption'].split(':')
    user_id = int(_user_id)
    reply_msg_id = int(_reply_msg_id)
    if 'video' in message:
        await _bot.send_video(user_id, message['video']['file_id'], reply_to_message_id=reply_msg_id)
    elif 'audio' in message:
        await _bot.send_audio(user_id, message['audio']['file_id'], reply_to_message_id=reply_msg_id)
    elif 'document' in message:
        await _bot.send_document(user_id, message['document']['file_id'], reply_to_message_id=reply_msg_id)


async def _on_message_task(message):
    async with bot.action(message['chat']['id'], 'file'):
        chat_id = message['from']['id']
        msg_id = message['message_id']
        log = new_logger(chat_id, msg_id)
        try:
            await _on_message(message, log)
        except HTTPError as e:
            # crashing to try change ip
            # otherwise youtube.com will not allow us
            # to download any video for some time
            if e.response.status_code == 429:
                log.critical(e)
                os.abort()
        except Exception as e:
            log.exception(e)
            await bot.send_message(chat_id, e.__str__(), reply_to=msg_id)

# extract telegram command from message
def cmd_from_message(message):
    cmd = None
    if 'entities' in message:
        for e in message['entities']:
            if e['type'] == 'bot_command':
                cmd = message['text'][e['offset']+1:e['length']]

    return cmd


# convert each key-value to string like "key: value"
def dict_to_list(_dict):
    ret = []
    for k, v in _dict.items():
        ret.append(k+": "+v)

    return ret


async def extract_url_info(url, params):
    # data = {
    #     "url": url,
    #     **params
    # }
    # headers = {
    #     "x-ibm-client-id": YTDL_LAMBDA_SECRET
    # }
    # async with ClientSession() as session:
    #     async with session.post(YTDL_LAMBDA_URL, json=data, headers=headers, timeout=14400) as req:
    #         return await req.json()
    ydl = youtube_dl.YoutubeDL(params=params)
    return await asyncio.get_event_loop().run_in_executor(None, ydl.extract_info, url, False)

async def _on_message(message, log):
    if message['from']['is_bot']:
        log.info('Message from bot, skip')
        return

    msg_id = message['message_id']
    chat_id = message['chat']['id']
    msg_txt = message['text']

    log.info('message: ' + msg_txt)

    urls = url_extractor.find_urls(msg_txt)
    cmd = cmd_from_message(message)
    playlist_start = None
    playlist_end = None
    y_format = None
    # check cmd and choose video format
    if cmd is not None:
        if cmd not in available_cmds:
            await bot.send_message(chat_id, 'Wrong command', reply_to=msg_id)
            return
        elif cmd == 'start':
            await bot.send_message(chat_id, 'Send me a video links')
            return
        elif cmd == 'ping':
            await bot.send_message(chat_id, 'pong')
            return
        elif cmd in playlist_cmds:
            urls_count = len(urls)
            if urls_count != 1:
                await bot.send_message(chat_id, 'Please send one playlist url', reply_to=msg_id)
                return
            range_match = playlist_range_re.search(msg_txt)
            if range_match is None:
                await bot.send_message(chat_id,
                                       'Wrong message format, correct example: /' + cmd + " 4-9 " + urls[0],
                                       reply_to=msg_id)
                return
            _start, _end = range_match.groups()
            playlist_start = int(_start)
            playlist_end = int(_end)
            if playlist_start >= playlist_end:
                await bot.send_message(chat_id,
                                       'Not correct format, start number must be less then end',
                                       reply_to=msg_id)
                return
            elif playlist_end - playlist_start > 50:
                await bot.send_message(chat_id,
                                       'Too big range. Allowed range is less or equal 50 videos',
                                       reply_to=msg_id)
                return
            # cut "p" from cmd variable if cmd == "pa" or "pw"
            cmd = cmd if len(cmd) == 1 else cmd[-1]
        if cmd == 'a':
            # audio cmd
            y_format = audio_format
        elif cmd == 'w':
            # wordst video cmd
            y_format = worst_video_format

    if y_format == None:
        y_format = vid_format

    if len(urls) == 0:
        log.info('Message without url: ' + msg_txt)
        await bot.send_message(chat_id, 'Please send me link to the video', reply_to=msg_id)
        return

    for u in urls:
        params = {'format': y_format,
                  'noplaylist': True,
                  'youtube_include_dash_manifest': False,
                  'quiet': True,
                  'no_color': True}
        if playlist_start != None and playlist_end != None:
            if playlist_start == 0 and playlist_end == 0:
                params['playliststart'] = 1
                params['playlistend'] = 10
            else:
                params['playliststart'] = playlist_start
                params['playlistend'] = playlist_end
        else:
            params['playlist_items'] = '1'

        try:
            vinfo = await extract_url_info(u, params)
            log.debug('video info received')
        except Exception as e:
            if "Please log in or sign up to view this video" in str(e):
                if 'vk.com' in u:
                        params['username'] = os.environ['VIDEO_ACCOUNT_USERNAME']
                        params['password'] = os.environ['VIDEO_ACCOUNT_PASSWORD']
                        try:
                            vinfo = await extract_url_info(u, params)
                        except Exception as e:
                            log.error(e)
                            await bot.send_message(chat_id, str(e), reply_to=msg_id)
                            continue
                else:
                    log.error(e)
                    await bot.send_message(chat_id, str(e), reply_to=msg_id)
                    continue
            elif 'are video-only' in str(e):
                params['format'] = 'bestvideo[ext=mp4]'
                try:
                    vinfo = await extract_url_info(u, params)
                except Exception as e:
                    log.error(e)
                    await bot.send_message(chat_id, str(e), reply_to=msg_id)
                    continue
            else:
                log.error(e)
                await bot.send_message(chat_id, str(e), reply_to=msg_id)
                continue

        entries = None
        if '_type' in vinfo and vinfo['_type'] == 'playlist':
            entries = vinfo['entries']
        else:
            entries = [vinfo]

        for entry in entries:
            formats = entry.get('requested_formats')
            file_size = None
            chosen_format = None
            ffmpeg_av = None
            http_headers = None
            if 'http_headers' not in entry:
                if len(formats) > 0 and 'http_headers' in formats[0]:
                        http_headers = formats[0]['http_headers']
            else:
                http_headers = entry['http_headers']

            if formats is not None:
                for i, f in enumerate(formats):
                    if f['protocol'] in ['rtsp', 'rtmp', 'rtmpe', 'mms', 'f4m', 'ism', 'http_dash_segments']:
                        continue
                    if 'm3u8' in f['protocol']:
                        file_size = await av_utils.m3u8_video_size(f['url'], http_headers)
                    else:
                        if 'filesize' in f and f['filesize'] != 0 and f['filesize'] is not None:
                            file_size = f['filesize']
                        else:
                            file_size = await av_utils.media_size(f['url'], http_headers=http_headers)

                    # Dash video
                    if f['protocol'] == 'https' and \
                            (True if ('acodec' in f and (f['acodec'] == 'none' or f['acodec'] == None)) else False):
                        vformat = f
                        mformat = None
                        vsize = 0
                        if 'filesize' in vformat and vformat['filesize'] != 0 and vformat['filesize'] is not None:
                            vsize = vformat['filesize']
                        else:
                            vsize = await av_utils.media_size(vformat['url'], http_headers=http_headers)
                        msize = 0
                        # if there is one more format than
                        # it's likely an url to audio
                        if len(formats) > i+1:
                            mformat = formats[i+1]
                            if 'filesize' in mformat and mformat['filesize'] != 0 and mformat['filesize'] is not None:
                                msize = mformat['filesize']
                            else:
                                msize = await av_utils.media_size(mformat['url'], http_headers=http_headers)
                        file_size = vsize + msize + 10*1024*1024
                        if file_size/(1024*1024) < TG_MAX_FILE_SIZE:
                            ffmpeg_av = await av_source.FFMpegAV.create(vformat,
                                                                        mformat,
                                                                        headers=dict_to_list(http_headers))
                            chosen_format = f
                        break
                    # m3u8
                    if ('m3u8' in f['protocol'] and file_size / (1024*1024) <= TG_MAX_FILE_SIZE):
                        chosen_format = f
                        ffmpeg_av = await av_source.FFMpegAV.create(chosen_format,
                                                                    audio_only=True if cmd == 'a' else False,
                                                                    headers=dict_to_list(http_headers))
                        break
                    # regular video stream
                    if file_size / (1024 * 1024) <= TG_MAX_FILE_SIZE:
                        chosen_format = f
                        if cmd == 'a' and not (chosen_format['ext'] == 'mp3'):
                            ffmpeg_av = await av_source.FFMpegAV.create(chosen_format,
                                                                        audio_only=True,
                                                                        headers=dict_to_list(http_headers))
                        break

            else:
                if entry['protocol'] in ['rtsp', 'rtmp', 'rtmpe', 'mms', 'f4m', 'ism', 'http_dash_segments']:
                    await bot.send_message(chat_id, "ERROR: Failed find suitable video format", reply_to=msg_id)
                    continue
                if 'm3u8' in entry['protocol']:
                    file_size = await av_utils.m3u8_video_size(entry['url'], http_headers=http_headers)
                else:
                    if 'filesize' in entry and entry['filesize'] != 0 and entry['filesize'] is not None:
                        file_size = entry['filesize']
                    else:
                        file_size = await av_utils.media_size(entry['url'], http_headers=http_headers)
                if ('m3u8' in entry['protocol'] and file_size / (1024*1024) <= TG_MAX_FILE_SIZE):
                    chosen_format = entry
                    ffmpeg_av = await av_source.FFMpegAV.create(chosen_format,
                                                                audio_only=True if cmd == 'a' else False,
                                                                headers=dict_to_list(http_headers))
                elif (file_size / (1024 * 1024) <= TG_MAX_FILE_SIZE):
                    chosen_format = entry
                    if cmd == 'a' and not (chosen_format['ext'] == 'mp3'):
                        ffmpeg_av = await av_source.FFMpegAV.create(chosen_format,
                                                                    audio_only=True,
                                                                    headers=dict_to_list(http_headers))

            try:
                if chosen_format is None and ffmpeg_av is None:
                    await bot.send_message(chat_id, "ERROR: Failed find suitable video format", reply_to=msg_id)
                    continue
                if chosen_format['ext'] == 'unknown_video':
                    mime = await av_utils.media_mime(chosen_format['url'], http_headers=http_headers)
                    ext = mimetypes.guess_extension(mime)
                    if ext is None or ext == '':
                        await bot.send_message(chat_id, "ERROR: Failed find suitable video format", reply_to=msg_id)
                        continue
                    else:
                        ext = ext[1:]
                        if mime.split('/')[0] == 'audio' and ext == 'webm':
                            # telegram treat webm audio as video
                            # so use ogg ext to force audio
                            chosen_format['ext'] = 'ogg'
                        else:
                            chosen_format['ext'] = ext
                if cmd == 'a':
                    # we don't know real size due to converting formats
                    # so increase it in case of real size is less large then estimated
                    file_size += 200000

                log.debug('uploading file')
                upload_file = ffmpeg_av if ffmpeg_av is not None else await av_source.URLav.create(chosen_format['url'],
                                                                                                   http_headers)
                file_name = entry['title'] + '.' + \
                            (chosen_format['ext'] if ffmpeg_av is None or ffmpeg_av.format is None else ffmpeg_av.format)
                file = await client.upload_file(upload_file,
                                                file_name=file_name,
                                                file_size=file_size,
                                                http_headers=http_headers)

                width = height = duration = None
                if cmd == 'a':
                    if ('duration' not in entry and 'duration' not in chosen_format):
                        # info = await av_utils.av_info(chosen_format['url'],
                        #                               use_m3u8=('m3u8' in chosen_format['protocol']))
                        info = await av_utils.av_info(chosen_format['url'], http_headers=http_headers)
                        duration = int(float(info['format']['duration']))
                    else:
                        duration = int(entry['duration']) if 'duration' not in entry else int(entry['duration'])

                elif ('duration' not in entry and 'duration' not in chosen_format) or \
                        ('width' not in chosen_format) or ('height' not in chosen_format):
                    # info =  await av_utils.av_info(chosen_format['url'],
                    #                                use_m3u8=('m3u8' in chosen_format['protocol']))
                    info = await av_utils.av_info(chosen_format['url'])
                    width = info['streams'][0]['width']
                    height = info['streams'][0]['height']
                    duration = info['format']['duration']
                else:
                    width, height, duration = chosen_format['width'], chosen_format['height'], \
                                              int(entry['duration']) if 'duration' not in entry else int(entry['duration'])

                if upload_file is not None:
                    if inspect.iscoroutinefunction(upload_file.close):
                        await upload_file.close()
                    else:
                        upload_file.close()

                attributes = None
                if cmd == 'a':
                    performer = entry['artist'] if ('artist' in entry) and \
                                                   (entry['artist'] is not None) else None
                    title = entry['alt_title'] if ('alt_title' in entry) and \
                                                  (entry['alt_title'] is not None) else entry['title']
                    attributes = DocumentAttributeAudio(duration, title=title, performer=performer)
                else:
                    attributes = DocumentAttributeVideo(duration,
                                                        width,
                                                        height,
                                                        supports_streaming=False if ffmpeg_av is not None else True)
                force_document = False
                if ffmpeg_av is None and (chosen_format['ext'] != 'mp4' and cmd != 'a'):
                        force_document = True
                log.debug('sending file')
                video_note = False if cmd == 'a' or force_document else True
                voice_note = True if cmd == 'a' else False
                attributes = ((attributes,) if not force_document else None)

                await client.send_file(bot_entity, file,
                                       video_note=video_note,
                                       voice_note=voice_note,
                                       attributes=attributes,
                                       caption=str(chat_id) + ":" + str(msg_id),
                                       force_document=force_document)
            except Exception as e:
                print(e)
                traceback.print_exc()

api_id = int(os.environ['API_ID'])
api_hash = os.environ['API_HASH']

BOT_AGENT_CHAT_ID = int(os.environ['BOT_AGENT_CHAT_ID'])

# YTDL_LAMBDA_URL = os.environ['YTDL_LAMBDA_URL']
# YTDL_LAMBDA_SECRET = os.environ['YTDL_LAMBDA_SECRET']

client = TelegramClient(StringSession(get_client_session()), api_id, api_hash)
bot = TelegramClient('bot', api_id, api_hash).start(bot_token=os.environ['BOT_TOKEN'])
_bot = Bot(token=os.environ['BOT_TOKEN'])
bot_entity = None

vid_format = '((best[ext=mp4,height<=1080]+best[ext=mp4,height<=480])[protocol^=http]/best[ext=mp4,height<=1080]+best[ext=mp4,height<=480]/best[ext=mp4]+worst[ext=mp4]/best[ext=mp4]/(bestvideo[ext=mp4,height<=1080]+(bestaudio[ext=mp3]/bestaudio[ext=m4a]))[protocol^=http]/bestvideo[ext=mp4]+(bestaudio[ext=mp3]/bestaudio[ext=m4a]/bestaudio[ext=mp4])/best)[protocol!=http_dash_segments]'
worst_video_format = 'best[ext=mp4,height<=360]/bestvideo[ext=mp4,height<=360]+bestaudio[ext=m4a]/best'
audio_format = '((bestaudio[ext=m4a]/bestaudio[ext=mp3])[protocol^=http]/bestaudio/best[ext=mp4,height<=480]/best[ext=mp4]/best)[protocol!=http_dash_segments]'

url_extractor = URLExtract()

playlist_range_re = re.compile('([0-9]+)-([0-9]+)')
playlist_cmds = ['p', 'pa', 'pw']
available_cmds = ['start', 'ping', 'a', 'w'] + playlist_cmds

TG_MAX_FILE_SIZE = 1500


async def init_bot_enitty():
    global bot_entity
    bot_entity = await client.get_input_entity(os.environ['CHAT_WITH_BOT_ID'])

if __name__ == '__main__':
    app = web.Application()
    app.add_routes([web.post('/bot', on_message)])
    client.start()
    asyncio.get_event_loop().create_task(bot._run_until_disconnected())
    asyncio.get_event_loop().create_task(init_bot_enitty())
    asyncio.get_event_loop().create_task(web.run_app(app))
    client.run_until_disconnected()
