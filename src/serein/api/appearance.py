"""Instance-owned pictures, separate from model settings and memory content."""
import base64
import binascii
import json
import hashlib
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response

from ..core.store import Store, encode

MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 3 * 1024 * 1024
PREFIX = 'appearance_image:'
KEYS = ('user', 'assistant', 'hero')


def validate_image(value):
    if not isinstance(value, str) or len(value) > MAX_REQUEST_BYTES:
        raise ValueError('图片未保存：文件过大或格式不正确。')
    header, separator, encoded = value.partition(',')
    formats = {'data:image/png;base64': lambda b: b.startswith(b'\x89PNG\r\n\x1a\n'),
               'data:image/jpeg;base64': lambda b: b.startswith(b'\xff\xd8\xff') and b.endswith(b'\xff\xd9'),
               'data:image/webp;base64': lambda b: b.startswith(b'RIFF') and b[8:12] == b'WEBP',
               'data:image/gif;base64': lambda b: b.startswith((b'GIF87a', b'GIF89a'))}
    if not separator or header not in formats:
        raise ValueError('图片未保存：请使用 PNG、JPEG、WebP 或 GIF。')
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('图片未保存：文件编码不正确。') from None
    if not raw or len(raw) > MAX_IMAGE_BYTES or not formats[header](raw):
        raise ValueError('图片未保存：文件过大或格式不正确。')
    return value


def routes(settings, auth):
    router = APIRouter(dependencies=auth)

    @router.get('/v1/appearance/images')
    def read(response: Response, metadata: bool = False):
        response.headers['Cache-Control'] = 'no-store'
        with Store(settings.database, read_only=True) as store:
            rows = store.conn.execute('SELECT name,value_json FROM background_state WHERE name IN (?,?,?)',
                                      [PREFIX + key for key in KEYS]).fetchall()
        images = {row['name'][len(PREFIX):]: json.loads(row['value_json']) for row in rows}
        if metadata:
            images = {key: hashlib.sha256(value.encode()).hexdigest() for key, value in images.items()}
        return {'images': images}

    @router.get('/v1/appearance/images/{key}')
    def read_one(key: Literal['user', 'assistant', 'hero'], response: Response, version: str = ''):
        response.headers['Cache-Control'] = 'no-store'
        with Store(settings.database, read_only=True) as store:
            row = store.conn.execute('SELECT value_json FROM background_state WHERE name=?', (PREFIX + key,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Image not found')
        value = json.loads(row['value_json'])
        digest = hashlib.sha256(value.encode()).hexdigest()
        if version and version != digest:
            raise HTTPException(404, 'Image version no longer available')
        return {'data_url': value, 'version': digest}

    @router.put('/v1/appearance/images/{key}')
    async def save(key: Literal['user', 'assistant', 'hero'], request: Request, response: Response):
        if not settings.writable:
            raise HTTPException(403, 'This instance is read-only')
        chunks = []; size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_REQUEST_BYTES:
                raise HTTPException(413, '图片未保存：文件过大。')
            chunks.append(chunk)
        try:
            body = json.loads(b''.join(chunks))
            if not isinstance(body, dict) or set(body) != {'data_url'}:
                raise ValueError('图片上传格式不正确。')
            value = validate_image(body['data_url'])
        except (ValueError, UnicodeError) as error:
            raise HTTPException(400, str(error)) from None
        with Store(settings.database) as store, store.transaction():
            store.conn.execute('INSERT INTO background_state(name,value_json) VALUES (?,?) '
                               'ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json',
                               (PREFIX + key, encode(value)))
        response.headers['Cache-Control'] = 'no-store'
        return {'key': key, 'data_url': value}

    return router
