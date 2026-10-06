/// <reference path="../.echo-sdk/echo-workshop-plugin.d.ts" />

const mockPlaylist = { id: '0123456789abcdef0123456789abcdef', name: 'SDK fixture',
  tracks: [{ title: 'Authorized test', artists: ['SDK fixture'], album: null, durationMs: null }] };
const bridge = async (method, path, body) => {
  try { return await echo.trusted.invoke('bridge-request', { method, path, body }); }
  catch (error) {
    if (!String(error).includes('mock host does not execute full-trust code')) throw error;
    if (path === '/health') return { ok: true, mockOnly: true };
    if (path === '/metadata/lookup') return {source: null};
    if (path === '/playback/queue') return {queued: 0};
    if (path === '/playlists') return [mockPlaylist];
    if (path === '/resolve') return { url: 'https://audio.example.invalid/fixture.mp3' };
    if (path === '/source/resolve') return { url: 'https://audio.example.invalid/fixture.mp3', title: 'Authorized test', artist: 'SDK fixture' };
    if (path === '/resolve/start') return { jobId: 'a'.repeat(64) };
    if (path.startsWith('/resolve/status/')) return { state: 'ready', source: { url: 'https://audio.example.invalid/fixture.mp3',
      coverUrl: 'https://image.example.invalid/fixture.jpg', lyric: '[00:00.00]Authorized test', provider: 'SDK fixture' } };
    if (path === '/source/browse') return { tracks: [{ providerTrackId: mockPlaylist.id, kind: 'collection', title: mockPlaylist.name }], total: 1, hasMore: false };
    if (path === '/source/search' || path === '/source/collection') return {
      tracks: [{ providerTrackId: `${mockPlaylist.id}:0123456789abcdef`, kind: 'track', title: 'Authorized test', artist: 'SDK fixture' }],
      total: 1, hasMore: false,
    };
    if (path.startsWith('/playlists/')) return mockPlaylist;
    throw error;
  }
};

const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));
if (typeof setInterval === 'function') {
  let healthPending = false;
  const keepBridgeAvailable = async () => {
    if (healthPending) return;
    healthPending = true;
    try { await bridge('GET', '/health'); }
    catch (error) { console.warn('Portable Bridge recovery:', String(error)); }
    finally { healthPending = false; }
  };
  keepBridgeAvailable();
  setInterval(keepBridgeAvailable, 15000);
}
const direct = (track, source) => ({url: source.url, title: track.title,
  artist: track.artists.join(', '), album: track.album || undefined});
async function resolveTrack(track) {
  const {jobId} = await bridge('POST', '/resolve/start', {track});
  for (let attempt = 0; attempt < 120; attempt++) {
    const result = await bridge('GET', `/resolve/status/${jobId}`);
    if (result.state === 'ready') return direct(track, result.source);
    if (result.state === 'error') throw Error(result.error || '音源匹配失败');
    await wait(1000);
  }
  throw Error('音源匹配超过 120 秒');
}
const enrichment = new Map();
const trackMetadata = track => ({title: track.title,
  artists: String(track.artist).split(/[,、/]/).map(item => item.trim()).filter(Boolean),
  album: track.album || null});
async function enrichTrack(track, signal) {
  if (!track?.title || !track?.artist) return null;
  const wanted = trackMetadata(track);
  const key = JSON.stringify(wanted);
  let entry = enrichment.get(key);
  if (!entry || Date.now() - entry.at > entry.ttl) {
    const promise = bridge('POST', '/metadata/lookup', {track: wanted}).then(result => {
      if (!result.source?.lyric || !result.source?.coverUrl) entry.ttl = 2000;
      return result.source || null;
    });
    entry = {at: Date.now(), ttl: 30000, promise};
    enrichment.set(key, entry);
    if (enrichment.size > 32) enrichment.delete(enrichment.keys().next().value);
    promise.catch(() => enrichment.delete(key));
  }
  const result = await entry.promise;
  return signal?.aborted ? null : result;
}

let prefetchTimer = null;
let lastQueueKey = '';
async function warmPlaybackQueue() {
  const queue = await echo.queue.get();
  const current = queue.currentTrack;
  if (!current?.title || !current?.artist || current.mediaType === 'local') return;
  const index = queue.items.findIndex(item => item.queueId === queue.currentQueueId);
  // The SDK exposes no random-next identity; don't guess it in shuffle mode.
  const next = !queue.shuffleEnabled && index >= 0 ? queue.items.slice(index + 1, index + 3)
    .map(item => item.track).filter(track => track?.mediaType !== 'local' && track?.title && track?.artist) : [];
  const body = {current: trackMetadata(current), tracks: next.map(trackMetadata)};
  const key = JSON.stringify(body);
  if (key === lastQueueKey) return;
  await bridge('POST', '/playback/queue', body);
  lastQueueKey = key;
}
function scheduleQueueWarmup() {
  if (prefetchTimer !== null) return;
  prefetchTimer = setTimeout(() => {
    prefetchTimer = null;
    warmPlaybackQueue().catch(error => console.warn('Portable queue prefetch unavailable:', String(error)));
  }, 600);
}
let lastPlaybackTrackId = null;
let recoveryTrack = null;
let recoveryAttempted = false;
let recoveryPending = null;
let recoveryGeneration = 0;
let consecutiveSkips = 0;
let lastPlaybackState = null;
let playbackStartedAt = 0;
let timingReported = false;
function scheduleRecovery(trackId, generation) {
  const token = {trackId, generation};
  recoveryPending = token;
  setTimeout(() => {
    recoverPlayback(trackId, generation).catch(error => console.warn('Portable recovery:', String(error)))
      .finally(() => { if (recoveryPending === token) recoveryPending = null; });
  }, 1500);
}
async function recoverPlayback(trackId, generation) {
  const stillFailed = async () => {
    const current = await echo.playback.getStatus();
    return generation === recoveryGeneration && current.currentTrackId === trackId && current.state === 'error';
  };
  if (!await stillFailed()) return;
  const outcome = await bridge('POST', '/playback/recovery', {hostTrackId: trackId, retry: !recoveryAttempted});
  if (!outcome.owned || !outcome.failed || !await stillFailed()) return;
  if (!recoveryAttempted) {
    recoveryAttempted = true;
    try { await echo.playback.play(); } catch (error) { console.warn("Portable retry:", String(error)); }
    scheduleRecovery(trackId, generation);
  } else if (consecutiveSkips < 3) {
    consecutiveSkips++;
    await echo.playback.next();
    await echo.ui.notify('当前音源重试失败，已跳过此曲。');
  } else {
    await echo.ui.notify('连续三首无法播放，已停止自动跳过，请检查音源或歌单信息。');
  }
}
echo.events.on('playback:status', status => {
  if (status?.currentTrackId !== recoveryTrack || (status?.state === 'loading' && lastPlaybackState !== 'loading')) {
    playbackStartedAt = Date.now();
    timingReported = false;
  }
  if (!timingReported && typeof status?.currentTrackId === 'string' && status.currentTrackId.startsWith('remote:') &&
      (status.state === 'error' || (status.state === 'playing' && status.positionSeconds > 0))) {
    timingReported = true;
    bridge('POST', '/playback/telemetry', {hostTrackId: status.currentTrackId,
      elapsedMs: Date.now()-playbackStartedAt, outcome: status.state}).catch(()=>{});
  }

  if (status?.currentTrackId !== recoveryTrack) {
    recoveryTrack = status?.currentTrackId;
    lastPlaybackState = null;
    recoveryAttempted = false;
    recoveryGeneration++;
  }
  if (status?.state === 'playing' && status.positionSeconds > 2) consecutiveSkips = 0;
  if (status?.state === 'paused' || status?.state === 'stopped') recoveryGeneration++;
  if (status?.state === 'error' && lastPlaybackState !== 'error' &&
      (!recoveryPending || recoveryPending.trackId !== recoveryTrack || recoveryPending.generation !== recoveryGeneration)) {
    scheduleRecovery(recoveryTrack, recoveryGeneration);
  }
  lastPlaybackState = status?.state;

  if (typeof status?.currentTrackId !== 'string' || status.currentTrackId === lastPlaybackTrackId) return;
  lastPlaybackTrackId = status.currentTrackId;
  scheduleQueueWarmup();
});
echo.lyrics.registerProvider('portable-lyrics', {title: 'Afterecho Lyrics'}, async (request, context) => {
  const result = await enrichTrack(request.track, context?.signal);
  if (!result?.lyric) return {candidates: []};
  return {candidates: [{title: request.track.title, artist: request.track.artist, album: request.track.album,
    lrc: result.lyric, confidence: Math.min(1, (result.matchScore || 90) / 100), source: result.provider || 'MusicDL'}]};
});
echo.covers.registerProvider('portable-covers', {title: 'Afterecho Covers'}, async ({track}) => {
  const result = await enrichTrack(track);
  if (!result?.coverUrl) return {candidates: []};
  return {candidates: [{imageUrl: result.coverUrl, title: track.title,
    confidence: Math.min(1, (result.matchScore || 90) / 100), source: result.provider || 'MusicDL'}]};
});
let continuous = null;
async function nextTrack(state) {
  if (state.nextIndex >= state.total) return null;
  const index = state.nextIndex++;
  const page = Math.floor(index / 25) + 1;
  if (state.page !== page) {
    state.data = await bridge('POST', `/playlists/${state.playlistId}/tracks/page`, {page, pageSize: 25});
    state.page = page;
  }
  return state.data.tracks.find(item => item.index === index)?.track || null;
}
async function fillContinuous() {
  const state = continuous;
  if (!state || state.filling) return;
  state.filling = true;
  try {
    const queue = await echo.queue.get();
    if (queue.currentTrack && !state.trackIds.has(queue.currentTrack.id)) {
      continuous = null;
      return;
    }
    const current = queue.items.findIndex(item => item.queueId === queue.currentQueueId);
    let ahead = current < 0 ? queue.items.length : queue.items.length - current - 1;
    let attempted = 0;
    while (continuous === state && ahead < 3 && state.nextIndex < state.total && attempted++ < 8) {
      const track = await nextTrack(state);
      if (!track) break;
      try {
        const added = await echo.sources.enqueueDirect(await resolveTrack(track));
        if (added?.track?.id) state.trackIds.add(added.track.id);
        ahead++;
      }
      catch (error) { await echo.ui.notify(`连续播放跳过 ${track.title}：${error}`); }
    }
  } finally { state.filling = false; }
}
echo.commands.register('stop-continuous', {title: 'Stop Afterecho Continuous Play'}, () => {
  continuous = null;
  return {stopped: true};
});
echo.commands.register('start-continuous', {title: 'Play Afterecho Continuously'}, async (input) => {
  if (input == null) {
    await echo.ui.notify('请在 Afterecho 面板选择歌单并点击“连续播放”');
    return {started: false, reason: 'select a playlist in the Afterecho panel'};
  }
  const playlistId = input?.playlistId;
  const startIndex = input?.startIndex;
  if (typeof playlistId !== 'string' || !/^[a-f0-9]{32}$/.test(playlistId) ||
      !Number.isInteger(startIndex) || startIndex < 0 || startIndex >= 10000)
    return {started: false, reason: 'invalid playlist position'};
  const page = Math.floor(startIndex / 25) + 1;
  const data = await bridge('POST', `/playlists/${playlistId}/tracks/page`, {page, pageSize: 25});
  const track = data.tracks.find(item => item.index === startIndex)?.track;
  if (!track) throw Error('track not found in playlist');
  continuous = null;
  const played = await echo.sources.playDirect(await resolveTrack(track));
  continuous = {playlistId, nextIndex: startIndex + 1, total: data.total, page, data, filling: false,
    trackIds: new Set(played?.track?.id ? [played.track.id] : [])};
  await fillContinuous();
  return {started: true, total: data.total};
});
echo.events.on('queue:changed', () => {
  scheduleQueueWarmup();
  fillContinuous().catch(error => echo.ui.notify(`连续播放补队列失败：${error}`));
});
if (typeof setInterval === 'function') setInterval(() => {
  if (continuous) fillContinuous().catch(error => echo.ui.notify(`连续播放补队列失败：${error}`));
}, 15000);
echo.sources.registerProvider('portable-library', { title: 'Afterecho' }, {
  search: ({ query, page, pageSize }) => bridge('POST', '/source/search', { query, page, pageSize }),
  browse: ({ page, pageSize }) => bridge('POST', '/source/browse', { page, pageSize }),
  listCollection: ({ collectionId, page, pageSize }) =>
    bridge('POST', '/source/collection', { collectionId, page, pageSize }),
  resolve: ({ providerTrackId }) => bridge('POST', '/source/resolve', { providerTrackId }),
});

echo.commands.register('check-bridge', { title: 'Check Music Bridge' }, async () => {
  const health = await bridge('GET', '/health');
  await echo.ui.notify(health.ok ? 'Music Bridge connected' : 'Music Bridge unavailable');
  return health;
});
