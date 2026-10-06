"""First-run setup and supervised loopback service. No user data in release files."""
import json, os, sys, subprocess, time, urllib.request, msvcrt
from pathlib import Path
ROOT=Path(__file__).resolve().parent
HOME=Path(os.environ['ECHO_PORTABLE_HOME']).resolve()
DATA=HOME/'data'; DATA.mkdir(parents=True,exist_ok=True)
STATUS=HOME/'setup-status.json'
def status(state,message):
    tmp=HOME/('status-'+str(os.getpid())+'.tmp')
    tmp.write_text(json.dumps({'state':state,'message':message}),encoding='utf-8');os.replace(tmp,STATUS)
def main():
    lock=(HOME/'service.lock').open('a+b');lock.write(b'1');lock.flush();lock.seek(0)
    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:return
    try:
        if sys.version_info[:2]!=(3,12) or sys.maxsize<2**32:raise RuntimeError('需要 Python 3.12 64 位')
        envdir=ROOT/'venv';python=envdir/'Scripts/python.exe';ready=ROOT/'.ready'
        with (HOME/'setup.log').open('ab') as log:
            if not python.exists():
                status('installing','创建环境');subprocess.run([sys.executable,'-m','venv',str(envdir)],check=True,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
            if not ready.exists():
                status('installing','安装依赖，请稍候')
                subprocess.run([str(python),'-m','pip','--isolated','install','--disable-pip-version-check','--index-url','https://pypi.org/simple','-r',str(ROOT/'requirements.txt'),*(['-c',str(ROOT/'constraints.txt')] if (ROOT/'constraints.txt').exists() else [])],check=True,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
                subprocess.run([str(python),'-c','import fastapi,uvicorn,musicdl,opencc'],check=True,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
                ready.write_text('0.2.0',encoding='ascii')
        env=os.environ.copy();env.update(ECHO_PORTABLE_DATA=str(DATA),PYTHONUTF8='1',PYTHONIOENCODING='utf-8',ECHO_MUSICDL_SOURCES='KuwoMusicClient,QQMusicClient,NeteaseMusicClient,MiguMusicClient,QianqianMusicClient,FMAMusicClient')
        delay=1
        while True:
            status('starting','启动服务')
            with (HOME/'bridge.log').open('ab') as log:
                child=subprocess.Popen([str(python),'-m','uvicorn','app.main:app','--host','127.0.0.1','--port','18765','--no-access-log'],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
                for _ in range(80):
                    if child.poll() is not None:break
                    try:
                        with urllib.request.urlopen('http://127.0.0.1:18765/health',timeout=1) as res:h=json.load(res)
                        if h.get('service')=='echo-portable-library' and Path(h.get('dataDir','')).resolve()==DATA:
                            status('ready','服务就绪');break
                        raise RuntimeError('18765 端口已占用')
                    except (OSError,ValueError):time.sleep(.25)
                if child.poll() is not None:raise RuntimeError('启动失败，见 bridge.log')
                code=child.wait();status('starting','服务重启');time.sleep(delay);delay=min(30,delay*2)
    except subprocess.CalledProcessError:status('error','依赖安装失败，见 setup.log')
    except Exception as exc:status('error',str(exc))
if __name__=='__main__':main()
