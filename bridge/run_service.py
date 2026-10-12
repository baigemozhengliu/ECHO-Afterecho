"""Request graceful Uvicorn shutdown when the original ECHO process exits."""
import os
import threading
from lifecycle import HostProcess

def main():
    host = HostProcess(int(os.environ['ECHO_MAIN_PID']))
    if not host.alive(): return
    import uvicorn
    server = uvicorn.Server(uvicorn.Config('app.main:app', host='127.0.0.1', port=18765,
                                         access_log=False, timeout_graceful_shutdown=5))
    done = threading.Event()
    def watch():
        while not done.wait(.2):
            if not host.alive():
                server.should_exit = True
                return
    watcher = threading.Thread(target=watch, daemon=True); watcher.start()
    try: server.run()
    finally:
        done.set(); watcher.join(); host.close()

if __name__ == '__main__': main()
