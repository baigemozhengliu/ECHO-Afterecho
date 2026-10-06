const request=async(method,path,body)=>{try{return await echo.trusted.invoke('bridge-request',{method,path,body})}catch(error){throw Error(`${method} ${path}: ${error.message||error}`)}};
const $=id=>document.getElementById(id),node=(tag,text='')=>{const n=document.createElement(tag);n.textContent=text;return n};
const status=(text,error=false)=>{$('status').textContent=text;$('status').className=error?'error':''};
const button=(label,action)=>{const b=node('button',label);b.type='button';b.onclick=async()=>{b.disabled=true;try{await action()}catch(e){status(String(e),true)}finally{b.disabled=false}};return b};
let lists=[],active,current,page=1,mode='songs',query='',revision=0,editing=false,tiles=true,selected=new Set();
const labels={songs:'全部歌曲',album:'专辑',artist:'歌手',review:'待检查',unavailable:'未找到音源'};
function theme(c){const a=c.appearance||{};for(const[k,v]of Object.entries({bg:a.appBg,panel:a.panel,text:a.text,muted:a.muted,border:a.border,accent:a.accent,'accent-text':a.accentText}))if(v)document.documentElement.style.setProperty('--'+k,v)}
echo.ui.getContext().then(theme).catch(()=>{});echo.ui.onContextChanged(theme);
// Preserve native text selection and IME; do not cancel keyboard default actions.
for(const event of ['keydown','keyup','keypress'])document.addEventListener(event,e=>{if(e.target.matches('input,textarea,select'))e.stopPropagation()});
// ECHO sandbox allows scripts but not native form submission.
function bindActionForm(form, submit, action) {
 submit.type='button';
 let busy=false;
 const run=async()=>{if(busy||!form.reportValidity())return;busy=true;submit.disabled=true;try{await action({target:form,preventDefault(){}})}finally{busy=false;submit.disabled=false}};
 submit.onclick=run;
 form.onsubmit=e=>{e.preventDefault();return run()};
 form.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.isComposing&&e.keyCode!==229&&e.target.matches('input')){e.preventDefault();void run()}});
}
function modal(title){$('modal-title').textContent=title;$('modal-body').replaceChildren();if(!$('modal').open)$('modal').showModal();return $('modal-body')}
$('close-modal').onclick=()=>$('modal').close();
function selectionBar(){$('bulk-bar').hidden=!editing;$('selection-count').textContent=`已选 ${selected.size} 首`;for(const id of ['copy','move','delete-selected'])$(id).disabled=!selected.size;$('edit-mode').textContent=editing?'退出编辑':'编辑'}
function check(ids){const c=document.createElement('input');c.type='checkbox';c.checked=ids.every(id=>selected.has(id));c.indeterminate=!c.checked&&ids.some(id=>selected.has(id));c.setAttribute('aria-label','选择歌曲');c.onchange=()=>{for(const id of ids)c.checked?selected.add(id):selected.delete(id);selectionBar()};return c}
function args(){return {page,query,...(['review','unavailable'].includes(mode)?{state:mode}:{})}}
async function navigate(next,q=''){mode=next;query=q;page=1;$('query').value=q;await render()}
function sidebar(){
 $('playlists').replaceChildren();for(const l of lists){const b=button(l.name+(l.id===active?' · 启用':''),async()=>{current=l.id;selected.clear();query='';$('query').value='';page=1;await render()});b.className=l.id===current?'selected':'';$('playlists').append(b)}
 for(const [container,keys]of [['views',['songs','album','artist']],['checks',['review','unavailable']]]){$(container).replaceChildren();for(const key of keys){const b=button(labels[key],()=>navigate(key));b.className=mode===key?'selected':'';$(container).append(b)}}
 const list=lists.find(l=>l.id===current);$('playlist-tools').replaceChildren();if(list){$('playlist-tools').append(button('重命名',()=>rename(list)),button('导出歌单',()=>exportList(list)));if(current!==active)$('playlist-tools').append(button('删除歌单',()=>confirmAction(`删除「${list.name}」？`,'歌单及其条目将移除。',async()=>{await request('DELETE',`/playlists/${list.id}`);current=null;await refresh()})))}
}
async function refresh(){const [a,b]=await Promise.all([request('GET','/playlists/summary'),request('GET','/mount')]);lists=a;active=b.activePlaylistId;if(!lists.some(l=>l.id===current))current=active||lists[0]?.id;await render()}
async function render(){const run=++revision;sidebar();selectionBar();$('view-title').textContent=labels[mode];$('layout').hidden=!['album','artist'].includes(mode);$('layout').textContent=tiles?'☷ 列表':'▦ 方块';const list=lists.find(l=>l.id===current);$('actions').replaceChildren();if(!list){$('content').replaceChildren(node('p','新建歌单，开始整理你的音乐。'));return}
 $('list-title').textContent=list.name;$('mount-state').textContent=`${list.trackCount} 首 · ${current===active?'已启用 · 挂载到 ECHO':'未启用 · 仅保存在插件'}`;
 if(current!==active){const b=button('启用歌单',async()=>{await request('POST','/mount',{playlistId:list.id});await refresh();status('已切换挂载，请在 ECHO 远程连接执行完整同步。')});b.className='primary';$('actions').append(b)}
 const data=await request('POST',`/library/${current}/view`,{...args(),...(['album','artist'].includes(mode)?{group:mode}:{})});if(run!==revision)return;$('content').replaceChildren();$('pagination').replaceChildren();
 if(data.groups){const grid=node('div');grid.className='groups'+(tiles?'':' list');for(const g of data.groups){const card=node('div');card.className='group';const art=node('div',mode==='album'?'♫':'♬');art.className='art';card.append(art);const b=button(g.name,()=>navigate('songs',g.name));b.className='link';const text=node('div');text.append(b,node('small',`${g.count} 首`));card.append(text);if(editing)card.append(check(g.identities));grid.append(card)}$('content').append(grid);return}
 const table=node('table'),head=node('tr');for(const title of [...(editing?['']:[]),'歌曲','歌手','专辑'])head.append(node('th',title));if(editing)head.firstChild.className='check-cell';table.append(head);
 for(const row of data.rows){const tr=node('tr'),t=row.track;if(editing){const td=node('td');td.className='check-cell';td.append(check([row.identity]));tr.append(td)}const td=node('td'),b=button(t.title,()=>inspect(row));b.className='link';td.append(b,node('small',({matched:'已识别',review:'待检查',unavailable:'需重新解析'})[row.state]+(row.provider?' · '+providerName(row.provider):'')));tr.append(td);for(const v of [t.artists.join(', '),t.album||'未填写专辑']){const cell=node('td'),link=button(v,()=>navigate('songs',v));link.className='link';cell.append(link);tr.append(cell)}table.append(tr)}$('content').append(table);if(!data.rows.length)$('content').append(node('p','没有符合条件的歌曲。'));
 const prev=button('上一页',()=>{page--;return render()}),next=button('下一页',()=>{page++;return render()});prev.disabled=page===1;next.disabled=!data.hasMore;$('pagination').append(prev,node('span',`${data.total} 首 · 第 ${page} 页`),next);
}
function providerName(p){return {KuwoMusicClient:'酷我',NeteaseMusicClient:'网易云'}[p]||p.replace('MusicClient','')}
function confirmAction(title,description,action){const host=modal(title);host.append(node('p',description),button('确认',async()=>{await action();$('modal').close()}))}
function rename(list){const host=modal('重命名歌单'),input=document.createElement('input');input.value=list.name;input.setAttribute('aria-label','歌单名称');host.append(input,button('保存',async()=>{await request('POST',`/playlists/${list.id}/rename`,{name:input.value});$('modal').close();await refresh()}));input.focus()}
function inspect(row){const listId=current,host=$('inspector');host.replaceChildren(node('h3',row.track.title));const original=node('p',`原始输入：${row.original.title} — ${row.original.artists.join(', ')} / ${row.original.album||'未填写专辑'}`);original.className='original';host.append(original,node('small',row.provider?`当前来源：${providerName(row.provider)} · ${row.sourceId||''}`:'尚未识别来源'));const fields={};for(const[key,label,value]of [['title','歌名',row.track.title],['artists','歌手（逗号分隔）',row.track.artists.join(', ')],['album','专辑',row.track.album||'']]){const input=document.createElement('input');input.value=value;input.id='edit-'+key;const caption=node('label',label);caption.htmlFor=input.id;host.append(caption,input);fields[key]=input}
 host.append(button('保存修改',async()=>{const rows=await request('POST',`/library/${listId}/view`,{selectionOnly:true});if(!rows.identities.includes(row.identity))throw Error('歌曲已移除，请刷新');await request('POST',`/library/${listId}/edit`,{identity:row.identity,title:fields.title.value,artists:fields.artists.value.split(/[,，]/).map(x=>x.trim()).filter(Boolean),album:fields.album.value.trim()||null});await refresh();status('已保存。ECHO 显示更新需要同步。')}),button('补全信息',async()=>{status('正在检查来源元数据…');const r=await request('POST',`/library/${listId}/inspect`,{identity:row.identity});if(current===listId){await render();inspect({...row,track:r.track})}status(r.matched?'已补全可确认字段':'没有可确认的新信息，请重搜并选择正确录音')}),button('切换来源 / 重新搜索',()=>discovery(row,listId)));
}
function discovery(row=null,listId=current){if(!listId){status('请先新建歌单',true);return}const host=modal(row?'重新匹配歌曲':'搜索并添加'),form=node('form');form.className='search-form';const input=document.createElement('input');input.placeholder='输入歌名、歌手或专辑';input.setAttribute('aria-label','在线搜索');input.value=row?row.track.title:'';const provider=node('select');provider.setAttribute('aria-label','音源');for(const[v,t]of [['all','全部支持来源'],['KuwoMusicClient','酷我'],['NeteaseMusicClient','网易云']]){const o=node('option',t);o.value=v;provider.append(o)}const type=node('select');for(const[v,t]of [['song','单曲'],['album','专辑']]){const o=node('option',t);o.value=v;type.append(o)}type.disabled=!!row;const submit=node('button','搜索');submit.type='submit';form.append(input,provider,type,submit);const note=node('p','选择结果加入。专辑结果可能不全。');note.className='result-note';const results=node('div');host.append(form,note,results);input.focus();let generation=0;
 bindActionForm(form,submit,async e=>{e.preventDefault();const run=++generation;submit.disabled=true;results.replaceChildren(node('p','搜索中…'));try{const r=await request('POST','/discovery/search',{query:input.value,provider:provider.value});if(run!==generation)return;results.replaceChildren();const errors=Object.entries(r.errors).map(([k,v])=>`${providerName(k)}：${v}`).join('；');if(errors)results.append(node('p','部分来源请求未完成：'+errors));if(!r.candidates.length)results.append(node('p','本次未找到候选。可只搜歌名，或换一个来源；请求失败不表示歌曲不存在。'));
 const add=async candidate=>request('POST',`/library/${listId}/bind`,{token:candidate.token,...(row?{identity:row.identity}:{})});
 if(type.value==='album'){const groups=new Map();for(const c of r.candidates){const key=c.provider+'|'+(c.track.album||'未填写专辑');if(!groups.has(key))groups.set(key,[]);groups.get(key).push(c)}for(const members of groups.values()){const title=node('h3',`${members[0].track.album||'未填写专辑'} · ${providerName(members[0].provider)}`);results.append(title,button(`添加这 ${members.length} 首检索结果`,async()=>{let count=0;try{for(const c of members){await add(c);count++}status(`已处理 ${count} 首检索结果，重复来源不会重复加入`)}catch(e){status(`已处理 ${count} 首，其余未完成：${e}`,true)}finally{await refresh()}}));for(const c of members)resultRow(c)}}else for(const c of r.candidates)resultRow(c);
 function resultRow(c){const line=node('div');line.className='result';const text=node('div');text.append(node('strong',c.track.title),node('small',`${c.track.artists.join(', ')} · ${c.track.album||'未填写专辑'} · ${providerName(c.provider)}`));const b=button(row?'使用此录音':'加入歌单',async()=>{await add(c);await refresh();b.textContent=row?'已绑定':'已加入';b.onclick=null;status(row?'已切换解析来源，并保留原始输入。':'已加入目标歌单。');if(row){$('modal').close();$('inspector').replaceChildren(node('p','来源已更新，请重新选择歌曲查看详情。'))}});line.append(text,b);results.append(line)}
 }catch(err){results.replaceChildren(node('p',String(err)))}finally{submit.disabled=false}});
}
async function bulkTarget(action){const source=current,ids=[...selected];const host=modal(action==='move'?'移入歌单':'复制到歌单');host.append(node('p',`已选择 ${ids.length} 首歌曲`));const dest=node('div');dest.className='destination';const choose=l=>dest.append(button(l.name,async()=>{await request('POST',`/library/${source}/bulk`,{identities:ids,action,targetId:l.id});selected.clear();$('modal').close();await refresh();status('操作完成；挂载曲库变更后请同步 ECHO。')}));lists.filter(l=>l.id!==source).forEach(choose);const form=node('form'),input=document.createElement('input');input.placeholder='新歌单名称';input.required=true;const create=node('button','新建并使用');create.type='submit';form.append(input,create);bindActionForm(form,create,async e=>{e.preventDefault();create.disabled=true;try{const l=await request('POST','/playlists',{name:input.value});await request('POST',`/library/${source}/bulk`,{identities:ids,action,targetId:l.id});selected.clear();$('modal').close();await refresh()}catch(err){status(String(err),true)}finally{create.disabled=false}});host.append(dest,node('h3','或新建歌单'),form)}
$('new-list').onclick=()=>{$('create').hidden=false;$('create').elements.name.focus()};$('cancel-create').onclick=()=>{$('create').hidden=true};
bindActionForm($('create'),$('create').querySelector('.primary'),async e=>{e.preventDefault();try{const l=await request('POST','/playlists',{name:e.target.elements.name.value});current=l.id;selected.clear();e.target.reset();e.target.hidden=true;await refresh()}catch(err){status(String(err),true)}});
$('add-online').onclick=()=>discovery();$('layout').onclick=()=>{tiles=!tiles;render()};
for(const id of ['edit-mode','done-edit'])$(id).onclick=()=>{editing=!editing;selected.clear();render()};
for(const id of ['select-all','invert'])$(id).onclick=async()=>{try{const context=JSON.stringify([current,mode,query]),listId=current;const r=await request('POST',`/library/${listId}/view`,{...args(),selectionOnly:true});if(context!==JSON.stringify([current,mode,query]))return;for(const sid of r.identities)id==='invert'&&selected.has(sid)?selected.delete(sid):selected.add(sid);await render()}catch(e){status(String(e),true)}};
$('copy').onclick=()=>bulkTarget('copy');$('move').onclick=()=>bulkTarget('move');$('delete-selected').onclick=()=>{const listId=current,ids=[...selected];confirmAction('删除所选歌曲',`将从当前歌单移除 ${ids.length} 首。`,async()=>{await request('POST',`/library/${listId}/bulk`,{identities:ids,action:'delete'});selected.clear();await refresh();status('已删除。请在 ECHO 同步。空库残留需重建连接。')})};
let timer,composing=false;const search=()=>{if(composing)return;clearTimeout(timer);query=$('query').value;mode='songs';page=1;timer=setTimeout(()=>render().catch(e=>status(String(e),true)),250)};$('query').oncompositionstart=()=>{composing=true;clearTimeout(timer)};$('query').oncompositionend=()=>{composing=false;search()};$('query').oninput=search;
$('tips').onclick=async()=>{const host=modal('使用说明');host.append(node('pre','1 导入 TXT / JSON，或搜索添加。\n2 启用歌单。\n3 连接 ECHO → Subsonic → 同步。\n4 修改歌单后，再同步。\n\nTXT：每行一首，Tab 分列。\n歌名\t歌手\t专辑\n歌曲 A\t歌手甲 | 歌手乙\t专辑 A\n\n备份：侧栏 → 导出歌单。'));
try{const config=await echo.trusted.invoke('setup-status');host.append(node('p','数据目录'),node('pre',config.data),node('p','首次启动联网安装依赖。Python 3.12 64 位。'),node('p','个人研究使用。非 ECHO 官方插件。'),button('重试启动',async()=>{await echo.trusted.invoke('setup-retry');$('modal').close();waitForSetup()}))}catch(e){host.append(node('p',String(e)))}};
for(const [id,stop]of [['prepare-start',false],['prepare-stop',true]])$(id).onclick=async()=>{try{await request('POST',`/library/${current}/prepare`,stop?{stop:true}:{});$('prepare-status').textContent=stop?'当前查询结束后停止':'正在预解析，不下载音频'}catch(e){status(String(e),true)}};
setInterval(async()=>{const id=current;if(!id)return;try{const p=await request('GET',`/library/${id}/prepare`);if(id!==current)return;$('prepare-status').textContent=p.total!==undefined?`${p.running?'解析中':p.stop?'已停止':'已结束'} · ${p.checked}/${p.total}，已识别 ${p.matched} 首`:'保存歌曲身份与元数据，不批量下载音频'}catch{}},5000);

async function exportList(list){const data=await request('GET',`/playlists/${list.id}/metadata`);data.tracks=[];for(let p=1;;p++){const r=await request('POST',`/playlists/${list.id}/tracks/page`,{page:p,pageSize:100});data.tracks.push(...r.tracks.map(x=>x.track));if(!r.hasMore)break}const bytes=new TextEncoder().encode(JSON.stringify(data,null,2));let binary='';for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));await echo.files.export({suggestedName:list.name+'.json',mimeType:'application/json',dataBase64:btoa(binary)});}

$('import-file').onchange=async e=>{const file=e.target.files?.[0];if(!file)return;const list=lists.find(x=>x.id===current);if(!list){status('请先创建并选择目标歌单',true);return}let completed=0;try{const text=await file.text();const tracks=parseImport(text,file.name);
 if(tracks.length>10000)throw Error('单次最多导入 10000 首');if(!tracks.length)throw Error('文件中没有歌曲');
 for(let offset=0;offset<tracks.length;offset+=250){let result;for(let attempt=0;attempt<5;attempt++){try{result=await request('POST',`/playlists/${list.id}/tracks/batch`,{tracks:tracks.slice(offset,offset+250),offset:list.trackCount+offset});break}catch(error){if(!String(error).includes('rate-limited')||attempt===4)throw error;await new Promise(r=>setTimeout(r,2000*(attempt+1)))}}completed+=result.added;status(`导入到 ${list.name}：${completed}/${tracks.length}`)}await refresh();status(`已向 ${list.name} 添加 ${completed} 首；未自动切换挂载。`)}catch(error){status(`导入停止，已保留 ${completed} 首：${error}`,true)}finally{e.target.value=''}};
async function waitForSetup(){try{const state=await echo.trusted.invoke('setup-status');status(state.message,state.state==='error');if(state.state==='ready'){await refresh();return}if(state.state==='error')return}catch(e){status(String(e),true);return}setTimeout(waitForSetup,2000)}
waitForSetup();


function parseImport(text, filename){
 text=text.replace(/^\uFEFF/,'');
 if(filename.toLowerCase().endsWith('.json')){const d=JSON.parse(text);if(d.format!=='echo-portable-library'||d.version!==1||!Array.isArray(d.tracks))throw Error('需要 Afterecho v1 JSON');return d.tracks}
 return text.split(/\r?\n/).filter(line=>line.trim()).map((raw,i)=>{
  if(raw.includes('\t')){const parts=raw.split('\t').map(x=>x.trim());if(i===0&&parts[0]==='歌名'&&parts[1]==='歌手')return null;if(parts.length<2||parts.length>3||!parts[0])throw Error(`第 ${i+1} 行需要歌名、歌手、可选专辑三列`);return {title:parts[0],artists:parts[1]?parts[1].split('|').map(x=>x.trim()).filter(Boolean):['未知艺术家'],album:parts[2]||null}}
  const line=raw.trim();if(/^(?:[A-Za-z]:[\\/]|\/storage\/)/.test(line))throw Error(`第 ${i+1} 行是文件路径，请先清洗为三列 TXT`);
  const pos=line.lastIndexOf(' - ');if(pos<1||!line.slice(pos+3).trim())throw Error(`第 ${i+1} 行需要“歌名 - 歌手”或三列 Tab 格式`);return {title:line.slice(0,pos).trim(),artists:[line.slice(pos+3).trim()]}
 }).filter(Boolean);
}

$('connect-echo').onclick=async()=>{
 const host=modal('连接 ECHO 音乐库');host.append(node('p','正在读取本机连接设置…'));
 try{
  const config=await echo.trusted.invoke('connection-settings');
  host.replaceChildren(node('p','ECHO → 网盘 / 远程 → 新建 Subsonic → 填入下列参数 → 同步。'));
  for(const [label,value,secret] of [['服务器地址',config.url,false],['用户名',config.username,false],['密码',config.password,true]]){
   const line=node('div');line.className='result';const field=document.createElement('input');field.readOnly=true;field.value=value;field.type=secret?'password':'text';field.setAttribute('aria-label',label);
   const feedback=node('small');line.append(node('strong',label),field,button('复制',()=>{field.focus();field.select();try{if(!document.execCommand('copy'))throw Error('copy denied');feedback.textContent='已复制'}catch{feedback.textContent='已选中，请按 Ctrl+C 复制'}}));
   if(secret)line.append(button('显示 / 隐藏',()=>{field.type=field.type==='password'?'text':'password'}));host.append(line,feedback);
  }
  host.append(node('p','仅挂载启用歌单。修改后需同步。'));
 }catch(error){host.replaceChildren(node('p','读取连接设置失败：'+String(error)))}
};
