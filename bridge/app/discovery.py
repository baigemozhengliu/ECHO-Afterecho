"""Metadata candidate search: never fetch audio, report transport failure separately."""
import html
from concurrent.futures import ThreadPoolExecutor, wait
from threading import BoundedSemaphore
from .core import normalize_track

POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='management-search')
SLOTS = BoundedSemaphore(2)
PROVIDERS = {'KuwoMusicClient': '酷我', 'NeteaseMusicClient': '网易云'}

def search_candidates(musicdl, query, provider='all'):
    if provider not in ('all', *PROVIDERS):
        raise ValueError('unsupported search provider')
    def run(p):
        try:
            client = musicdl._client([p]).music_clients[p]
            client.search_size_per_page = 50
            client.search_size_per_source = 50
            req = client._constructsearchurls(keyword=query)[0]
            if p == 'KuwoMusicClient':
                response = client.get(req, timeout=(2, 4))
            else:
                response = client.post(req['url'], data=req['data'], timeout=(2, 4))
            response.raise_for_status()
            data = response.json()
            rows = data.get('abslist', []) if p == 'KuwoMusicClient' else data.get('result', {}).get('songs', [])
            results = []
            for row in rows:
                try:
                    if p == 'KuwoMusicClient':
                        identity = str(row.get('MUSICRID', '')).removeprefix('MUSIC_')
                        title, artist, album = [html.unescape(row.get(k) or '') for k in ('SONGNAME','ARTIST','ALBUM')]
                        artists = [artist or '未知艺术家']
                        duration = round(float(row.get('DURATION') or 0)*1000)
                    else:
                        identity = str(row['id']); title = row['name']
                        artists = [a['name'] for a in row.get('ar', [])] or ['未知艺术家']
                        album = row.get('al', {}).get('name'); duration = row.get('dt') or 0
                    if not identity: continue
                    track = normalize_track({'title':title,'artists':artists,'album':album or None,'durationMs':duration or None,'sourceHints':{p:identity}})
                    results.append({'provider':p,'sourceId':identity,'track':track})
                except (KeyError, ValueError, TypeError): continue
            return results, None
        except Exception as exc:
            return [], type(exc).__name__
        finally: SLOTS.release()
    jobs={}; errors={}
    for p in PROVIDERS:
        if p not in musicdl.sources or provider not in ('all', p): continue
        if SLOTS.acquire(blocking=False): jobs[POOL.submit(run,p)]=p
        else: errors[p]='busy'
    done,pending=wait(jobs,timeout=8) if jobs else ([],[])
    results=[]
    for future in done:
        rows,error=future.result();results.extend(rows)
        if error: errors[jobs[future]]=error
    for future in pending: errors[jobs[future]]='timeout'
    return {'candidates':results,'errors':errors,'completeTrackLists':False}
