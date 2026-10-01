#!/usr/bin/env python3
"""Browser QA against a local test deployment, using a supplied admin password."""
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright


def verify(url,password,output,exercise=False):
    output.mkdir(parents=True,exist_ok=True)
    errors=[];checks=[]
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1000},device_scale_factor=1)
        page.on('pageerror',lambda exc:errors.append(str(exc)))
        page.goto(url,wait_until='domcontentloaded')
        page.get_by_label('密码',exact=True).fill(password)
        page.get_by_role('button',name='登录',exact=True).click()
        page.get_by_role('heading',name='运行总览',exact=True).wait_for()
        if exercise:
            page.get_by_role('link',name='QQ 设置',exact=False).click()
            page.get_by_label('群白名单').fill('111\n222')
            page.get_by_label('姓名映射（JSON）').fill('{}')
            page.get_by_role('button',name='保存 QQ 配置').click()
            page.get_by_text('QQ 配置已保存',exact=True).wait_for()
            checks.append('QQ whitelist save')
        for path,title in [('/','运行总览'),('/qq','QQ 设置'),('/history','消息与任务'),('/memories','记忆与概念'),('/assets-library','素材收藏'),('/notes','笔记管理'),('/settings','模型与行为'),('/maintenance','日志与维护')]:
            page.goto(url.rstrip('/')+path,wait_until='domcontentloaded')
            page.get_by_role('heading',name=title,exact=True).wait_for()
            assert page.locator('[role="alert"]').count()==0,page.locator('[role="alert"]').all_text_contents()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),f'Desktop horizontal overflow: {path}'
            page.screenshot(path=output/((path.strip('/') or 'overview')+'.png'),full_page=True)
            checks.append(path+' desktop')
        if exercise:
            page.goto(url.rstrip('/')+'/memories',wait_until='domcontentloaded')
            page.get_by_role('button',name='添加记忆',exact=True).click()
            page.get_by_label('群号',exact=True).fill('111')
            page.get_by_label('内容',exact=True).fill('浏览器回归验证资料')
            page.get_by_role('button',name='保存记忆',exact=True).click()
            page.get_by_text('记忆已保存并同步索引',exact=True).wait_for()
            assert page.get_by_text('浏览器回归验证资料',exact=True).count()>0
            checks.append('Memory create in same data store')
            page.goto(url.rstrip('/')+'/notes',wait_until='domcontentloaded')
            page.locator('form').filter(has=page.get_by_role('button',name='追加保存',exact=True)).get_by_label('名称',exact=True).fill('浏览器验证')
            page.get_by_label('内容',exact=True).fill('检查第一条')
            page.get_by_role('button',name='追加保存',exact=True).click()
            page.get_by_text('笔记已保存',exact=True).wait_for()
            checks.append('Group note append')
        page.set_viewport_size({'width':390,'height':844})
        for path,title in [('/','运行总览'),('/history','消息与任务'),('/memories','记忆与概念'),('/settings','模型与行为')]:
            page.goto(url.rstrip('/')+path,wait_until='domcontentloaded')
            page.get_by_role('heading',name=title,exact=True).wait_for()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),f'Mobile horizontal overflow: {path}'
            page.screenshot(path=output/('mobile-'+(path.strip('/') or 'overview')+'.png'),full_page=True)
            checks.append(path+' mobile')
        browser.close()
    assert not errors,errors
    result={'checks':checks,'console_errors':errors}
    (output/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:18080')
    parser.add_argument('--output',type=Path,default=Path('/tmp/huiye-browser-results'))
    parser.add_argument('--exercise-admin',action='store_true',help='Write QQ config, memory and notes; only use on a temporary test deployment')
    args=parser.parse_args()
    if urlparse(args.url).hostname not in ('localhost','127.0.0.1','::1'):parser.error('Only local test deployments are accepted')
    password=os.environ.get('HUIYE_BROWSER_PASSWORD')
    if not password:parser.error('Set HUIYE_BROWSER_PASSWORD for the local test administrator')
    print(json.dumps(verify(args.url,password,args.output,args.exercise_admin),ensure_ascii=False,indent=2))
