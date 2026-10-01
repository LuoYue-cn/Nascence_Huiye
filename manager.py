"""Maintenance CLI using the authenticated management API, never direct DB writes."""
import argparse
import getpass
import json
import httpx

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['status','backup','rebuild_indexes','self_test'])
    parser.add_argument('--url',default='http://127.0.0.1:8000')
    args=parser.parse_args()
    with httpx.Client(base_url=args.url,timeout=60) as client:
        response=client.post('/api/v1/auth/login',json={'name':'admin','password':getpass.getpass('管理员密码：')})
        response.raise_for_status(); csrf=response.json()['csrf']
        if args.command=='status': response=client.get('/api/v1/status')
        else: response=client.post('/api/v1/maintenance',json={'kind':args.command},headers={'X-CSRF-Token':csrf})
        response.raise_for_status(); print(json.dumps(response.json(),ensure_ascii=False,indent=2))
        client.post('/api/v1/auth/logout',headers={'X-CSRF-Token':csrf})

if __name__=='__main__': main()
