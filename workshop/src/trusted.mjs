import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {execFile,spawn} from 'node:child_process';
import {promisify} from 'node:util';
import {createHash} from 'node:crypto';
const run=promisify(execFile);
const VERSION='0.2.0', BASE='http://127.0.0.1:18765';
const HOME=process.env.ECHO_PORTABLE_HOME||path.join(process.env.LOCALAPPDATA||path.join(os.homedir(),'AppData','Local'),'Afterecho');
const DATA=path.join(HOME,'data');
const BUNDLE_SHA='__BUNDLE_SHA__';
let context,starting=null,lastError='',started=false;
async function health(){try{const r=await fetch(BASE+'/health',{signal:AbortSignal.timeout(1000)});const h=await r.json();if(h.service!=='echo-portable-library'||path.resolve(h.dataDir||'')!==path.resolve(DATA))throw Error('18765 端口已占用');return h}catch(e){if(e.message==='18765 端口已占用')throw e;return null}}
async function python(){
 const candidates=process.env.ECHO_PORTABLE_PYTHON?[[process.env.ECHO_PORTABLE_PYTHON,[]]]:[['py',['-3.12']],['python',[]]];
 for(const [exe,args]of candidates){try{const {stdout}=await run(exe,[...args,'-c','import sys;assert sys.version_info[:2]==(3,12) and sys.maxsize>2**32;print(sys.executable)'],{windowsHide:true,timeout:10000});return stdout.trim()}catch{}}
 throw Error('请安装 Python 3.12 64 位，勾选 Add python.exe to PATH，再重启 ECHO');
}
async function launch(){
 if(await health())return;
 if(started)return;
 if(!starting)starting=(async()=>{
  const exe=await python();await fs.mkdir(HOME,{recursive:true});
  const bundle=path.join(context.contentRoot,'assets','bridge.data');
  const bytes=await fs.readFile(bundle);if(createHash('sha256').update(bytes).digest('hex')!==BUNDLE_SHA)throw Error('安装包校验失败');
  const root=path.join(HOME,'runtime',VERSION+'-'+BUNDLE_SHA.slice(0,12));
  const extract="import sys,zipfile,pathlib; p=pathlib.Path(sys.argv[2]).resolve();p.mkdir(parents=True,exist_ok=True);z=zipfile.ZipFile(sys.argv[1]);assert all(p in (p/n).resolve().parents for n in z.namelist());z.extractall(p)";
  await run(exe,['-c',extract,bundle,root],{windowsHide:true,timeout:20000});
  const child=spawn(exe,[path.join(root,'setup_runtime.py')],{cwd:root,env:{...process.env,ECHO_PORTABLE_HOME:HOME},windowsHide:true,detached:true,stdio:'ignore'});
  await new Promise((resolve,reject)=>{child.once('spawn',resolve);child.once('error',reject)});child.unref();started=true;lastError='';
 })().catch(e=>{lastError=e.message;throw e}).finally(()=>starting=null);
 return starting;
}
export function activate(value){context=value;void launch().catch(()=>{});}
export async function handle(request){
 if(request.method==='setup-retry'){started=false;lastError='';await launch();return {ok:true}}
 if(request.method==='setup-status'){
  const h=await health();if(h)return {state:'ready',message:'服务就绪',home:HOME,data:DATA};
  if(lastError)return {state:'error',message:lastError,home:HOME,data:DATA};
  await launch();try{return {...JSON.parse(await fs.readFile(path.join(HOME,'setup-status.json'),'utf8')),home:HOME,data:DATA}}catch{return {state:'starting',message:'准备环境',home:HOME,data:DATA}}
 }
 await launch();if(!await health())throw Error('环境准备中，请稍候');
 if(request.method==='connection-settings'){const auth=JSON.parse(await fs.readFile(path.join(DATA,'subsonic-auth.json'),'utf8'));return {url:BASE,username:auth.username,password:auth.password}}
 if(request.method!=='bridge-request')throw Error('unknown-method');
 const {method,path:route,body}=request.input||{};
 if(!['GET','POST','DELETE'].includes(method)||typeof route!=='string'||!ALLOWED.some(rule=>rule.test(route)))throw Error('bridge-request-not-allowed');
 const response=await fetch(BASE+route,{method,headers:{'content-type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(30000)});
 const payload=await response.json();if(!response.ok)throw Error(`bridge-${response.status}: ${payload.detail||'request failed'}`);
 if(Buffer.byteLength(JSON.stringify(payload))>262144)throw Error(`返回过大：${method} ${route}`);
 return payload;
}

const ALLOWED = [
  /^\/discovery\/search$/, /^\/mount$/, /^\/library\/[a-f0-9]{32}\/(view|inspect|prepare|bind|bulk|edit)$/,
  /^\/metadata\/lookup$/, /^\/playback\/(queue|recovery|telemetry)$/,
  /^\/health$/, /^\/search$/, /^\/search-album$/, /^\/albums\/search$/, /^\/resolve$/, /^\/resolve\/start$/, /^\/resolve\/status\/[a-f0-9]{64}$/, /^\/playlists$/, /^\/playlists\/summary$/, /^\/playlists\/import$/,
  /^\/source\/(search|browse|collection|resolve)$/,
  /^\/playlists\/[a-f0-9]{32}(?:\/tracks|\/tracks\/batch|\/tracks\/page|\/metadata|\/export|\/rename|\/move|\/tracks\/\d+|\/tracks\/\d+\/edit|\/albums\/merge)?$/,
];

