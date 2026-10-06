"""Small metadata-only requests. Never resolve or download audio here."""
from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import urlparse

from .core import match_score, unknown_artist


def clean_lyric(value: Any) -> str | None:
    if not isinstance(value, str) or value.strip().upper() in {'', 'NULL', 'NONE'}:
        return None
    return '\n'.join(line for line in value.splitlines() if not line.startswith('[kuwo:'))[:262144]


def clean_cover(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if value.startswith('//'):
        value = 'https:' + value
    parsed = urlparse(value)
    if '/starheads/' in parsed.path or '/wmvpic/' in parsed.path:
        # Kuwo search also returns artist portraits and video stills as "pic".
        return None
    return value if parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username and not parsed.password else None


def structured_lines(lyric: str) -> list[dict]:
    lines = []
    for raw in lyric.splitlines():
        stamps = list(re.finditer(r'\[(\d+):(\d+(?:\.\d+)?)\]', raw))
        if stamps:
            value = raw[stamps[-1].end():].strip()
            for stamp in stamps:
                lines.append({'start': round((int(stamp[1]) * 60 + float(stamp[2])) * 1000), 'value': value})
        elif raw.strip() and not re.match(r'^\[[a-zA-Z]+:', raw):
            lines.append({'value': raw.strip()})
    return sorted(lines, key=lambda line: line.get('start', 0))


def fetch_metadata(musicdl, wanted: dict, indexed: dict, identity_only: bool = False) -> dict:
    result = {key: indexed.get(key) for key in ('coverUrl', 'lyric', 'matchScore', 'durationMs', 'artists', 'provider', 'sourceId', 'title', 'artist', 'album')}
    result['lyric'] = clean_lyric(result['lyric'])
    result['coverUrl'] = clean_cover(result['coverUrl'])
    if identity_only and result.get('sourceId'):
        return {key: value for key, value in result.items() if value}
    providers = [p for p in ('KuwoMusicClient', 'NeteaseMusicClient') if p in musicdl.sources]
    preferred = indexed.get('provider')
    providers.sort(key=lambda p: p != preferred)
    for provider in providers:
        if not identity_only and result.get('coverUrl') and result.get('lyric') and result.get('durationMs'):
            break
        try:
            client = musicdl._client([provider]).music_clients[provider]
            identity = indexed.get('sourceId') if provider == preferred else None
            row = None
            score = indexed.get('matchScore', 90)
            if not identity:
                query = wanted['title'] + ' ' + ((wanted.get('album') or '') if unknown_artist(wanted['artists']) else ' '.join(wanted['artists']))
                search = client._constructsearchurls(keyword=query)[0]
                if provider == 'KuwoMusicClient':
                    response = client.get(search, timeout=(2, 3))
                    response.raise_for_status()
                    rows = response.json().get('abslist', [])
                    candidates = [(raw, {'title': html.unescape(raw.get('SONGNAME') or ''),
                        'artists': [html.unescape(raw.get('ARTIST') or '')],
                        'album': html.unescape(raw.get('ALBUM') or '')}) for raw in rows]
                else:
                    response = client.post(search['url'], data=search['data'], timeout=(2, 3))
                    response.raise_for_status()
                    rows = response.json().get('result', {}).get('songs', [])
                    candidates = [(raw, {'title': raw.get('name'),
                        'artists': [a['name'] for a in raw.get('ar', [])],
                        'album': raw.get('al', {}).get('name')}) for raw in rows]
                ranked = sorted(((match_score(wanted, track), raw, track) for raw, track in candidates), key=lambda pair: pair[0], reverse=True)
                if not ranked or ranked[0][0] < (85 if wanted.get('album') else 70):
                    continue
                threshold = 85 if wanted.get('album') else 70
                if unknown_artist(wanted['artists']) and len({tuple(t['artists']) for score, _, t in ranked if score >= threshold}) > 1:
                    continue
                score, row, matched = ranked[0]
                result['artists'] = matched['artists']
                result['matchScore'] = score
                raw_duration = row.get('DURATION') if provider == 'KuwoMusicClient' else row.get('dt')
                if raw_duration and str(raw_duration).replace('.', '', 1).isdigit():
                    result['durationMs'] = round(float(raw_duration) * (1000 if provider == 'KuwoMusicClient' else 1))
                identity = str(row.get('MUSICRID', '')).removeprefix('MUSIC_') if provider == 'KuwoMusicClient' else row['id']
                if not result.get('sourceId'):
                    result.update(provider=provider, sourceId=str(identity), title=matched['title'],
                                  artist=', '.join(matched['artists']), album=matched.get('album'))
                if identity_only:
                    return {key: value for key, value in result.items() if value}

            if provider == 'KuwoMusicClient':
                response = client.get('https://m.kuwo.cn/newh5/singles/songinfoandlrc',
                    params={'musicId': identity}, headers={'Referer': f'https://m.kuwo.cn/yinyue/{identity}',
                    'User-Agent': 'Mozilla/5.0'}, timeout=(2, 3))
                response.raise_for_status()
                data = response.json().get('data') or {}
                info = data.get('songinfo') or {}
                if provider == preferred:
                    if info.get('artist'): result['artists'] = [info['artist']]
                    if info.get('album'): result['album'] = info['album']
                cover = info.get('pic') or info.get('albumpic')
                duration = info.get('duration')
                if duration and str(duration).replace('.', '', 1).isdigit():
                    result['durationMs'] = round(float(duration) * 1000)
                lines = []
                for line in data.get('lrclist') or []:
                    stamp = round(float(line['time']) * 100)
                    lines.append(f'[{stamp // 6000:02d}:{stamp // 100 % 60:02d}.{stamp % 100:02d}]{line.get("lineLyric", "")}')
                lyric = '\n'.join(lines)
            else:
                if row is None:
                    import json
                    response = client.post('https://interface3.music.163.com/api/v3/song/detail',
                        data={'c': json.dumps([{'id': identity, 'v': 0}])}, timeout=(2, 3))
                    response.raise_for_status()
                    row = next(iter(response.json().get('songs') or []), {})
                if provider == preferred:
                    if row.get('ar'): result['artists'] = [a['name'] for a in row['ar'] if a.get('name')]
                    if row.get('al', {}).get('name'): result['album'] = row['al']['name']
                cover = row.get('al', {}).get('picUrl')
                if row.get('dt'):
                    result['durationMs'] = row['dt']
                lyric = None
                if not result.get('lyric'):
                    response = client.post('https://interface3.music.163.com/api/song/lyric',
                        data={'id': identity, 'lv': 0, 'tv': 0, 'rv': 0}, timeout=(2, 3))
                    response.raise_for_status()
                    lyric = response.json().get('lrc', {}).get('lyric')
            if not result.get('coverUrl'):
                result['coverUrl'] = clean_cover(cover)
            if not result.get('lyric'):
                result['lyric'] = clean_lyric(lyric)
            result['matchScore'] = score
        except Exception:
            # A metadata provider failure must never fail or delay audio playback.
            continue
    return {key: value for key, value in result.items() if value}
