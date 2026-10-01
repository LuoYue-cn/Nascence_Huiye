"""The only runtime entry point: QQ bot and management panel at /."""
import argparse
import logging
import os
from utils.paths import LOG_DIR

def run():
    parser=argparse.ArgumentParser(description='Nascence 辉夜 QQ bot 管理服务')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8000)
    args=parser.parse_args()
    LOG_DIR.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s',
                        handlers=[logging.StreamHandler(),logging.FileHandler(LOG_DIR/'service.log',encoding='utf-8')])
    from utils.logging import SecretFormatter
    for handler in logging.getLogger().handlers:
        handler.setFormatter(SecretFormatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    import uvicorn
    uvicorn.run('app.main:app',host=args.host,port=args.port,workers=1,
                timeout_graceful_shutdown=180,ws_max_size=1024*1024,proxy_headers=True,forwarded_allow_ips='127.0.0.1')

if __name__=='__main__': run()
