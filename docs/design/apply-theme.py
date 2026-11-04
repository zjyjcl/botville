#!/usr/bin/env python3
"""Inline the local v2 stylesheet and original SVG placeholders into each mockup."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent
# Original geometric drawings. Names document semantic SF Symbols equivalents only.
PATHS = {
    'message.fill': '<path d="M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-5 3v-3a2 2 0 0 1-2-2V7a3 3 0 0 1 3-3Z"/><path d="M7 9h10M7 13h7"/>',
    'person.2.fill': '<circle cx="9" cy="8" r="3"/><path d="M2 20v-3a5 5 0 0 1 5-5h4a5 5 0 0 1 5 5v3H2ZM17 5a3 3 0 0 1 0 6M19 13a5 5 0 0 1 3 5v2h-3"/>',
    'safari': '<circle cx="12" cy="12" r="9"/><path d="m16 7-2 7-6 3 2-7 6-3ZM10 10l4 4"/>',
    'person.crop.circle': '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="9" r="3"/><path d="M5.5 18a7 7 0 0 1 13 0"/>',
    'faceid': '<path d="M3 8V4a1 1 0 0 1 1-1h4M16 3h4a1 1 0 0 1 1 1v4M21 16v4a1 1 0 0 1-1 1h-4M8 21H4a1 1 0 0 1-1-1v-4M8 8v3M16 8v3M12 9v5h-2M8 16q4 4 8 0"/>',
    'checkmark.seal': '<path d="m12 2 3 2 4 .5.5 4L22 12l-2.5 3.5-.5 4-4 .5-3 2-3-2-4-.5-.5-4L2 12l2.5-3.5.5-4L9 4l3-2Z"/><path d="m8 12 3 3 5-6"/>',
    'checkmark': '<path d="m5 12 4 4L19 6"/>',
    'lock.fill': '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3"/>',
    'bell': '<path d="M5 16V9a7 7 0 0 1 14 0v7l2 2H3l2-2ZM9 21h6M12 2V1"/>',
    'chevron.right': '<path d="m9 5 7 7-7 7"/>',
    'arrow.left': '<path d="m14 5-7 7 7 7"/>',
    'ellipsis': '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
    'plus': '<path d="M12 4v16M4 12h16"/>',
    'photo': '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8" cy="9" r="2"/><path d="m3 17 5-4 4 3 4-6 5 7"/>',
    'magnifyingglass': '<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
    'square.grid.2x2': '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    'link': '<path d="m10 8 3-3a4 4 0 0 1 6 6l-3 3M14 16l-3 3a4 4 0 0 1-6-6l3-3M8 16l8-8"/>',
    'gearshape': '<path d="m9 3 6 0 1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1 1-3Z"/><circle cx="12" cy="12" r="3"/>',
    'heart': '<path d="M12 20 4 12C-2 5 8 1 12 7c4-6 14-2 8 5l-8 8Z"/>',
    'waveform': '<path d="M3 9v6M7 5v14M11 2v20M15 7v10M19 4v16M23 10v4"/>',
    'face.smiling': '<circle cx="12" cy="12" r="9"/><path d="M8 8v1M16 8v1M7 14q5 6 10 0"/>',
    'antenna.radiowaves.left.and.right': '<path d="M3 18v-3M8 18v-6M13 18V9M18 18V6"/>',
    'wifi': '<path d="M2 7q10-8 20 0M5 11q7-6 14 0M9 15q3-3 6 0M12 19h.01"/>',
    'battery.100': '<rect x="1" y="6" width="19" height="12" rx="2"/><path d="M23 10v4"/><path d="M4 9h13v6H4Z" fill="currentColor" stroke="none"/>',
}

def icon(name, extra=''):
    if name not in PATHS:
        raise ValueError('Missing original SVG: '+name)
    return f'<svg class="icon {extra}" data-symbol="{name}" viewBox="0 0 24 24" aria-hidden="true">{PATHS[name]}</svg>'

def tabs(active):
    labels = [('消息','message.fill'),('通讯录','person.2.fill'),('发现','safari'),('我','person.crop.circle')]
    return '<nav class="tabs" aria-label="底部导航">'+''.join(f'<div class="tab{" active" if label==active else ""}">{icon(symbol)}<span>{label}</span></div>' for label,symbol in labels)+'</nav>'

def apply(path):
    s = path.read_text()
    n = int(re.search(r'name="design-frame-count" content="(\d+)"',s)[1])
    css = (ROOT/'design-system.css').read_text()+f'\n.sheet{{width:{390*n+24*(n-1)}px}}\n'
    s = re.sub(r'<style>.*?</style>',lambda _: '<style>\n'+css+'</style>',s,flags=re.S)
    s = re.sub(r'<div class="statusbar">.*?</div>',lambda _: '<div class="statusbar"><span>9:41</span><span class="status-icons">'+icon('antenna.radiowaves.left.and.right')+icon('wifi')+icon('battery.100','battery')+'</span></div>',s,flags=re.S)
    def header(m):
        text=m[0]
        title=re.search(r'<h2>(.*?)</h2>',text,re.S)[1]
        small=re.search(r'<small>(.*?)</small>',text,re.S)
        left='' if title in ['消息','发现','我','设计规范','组件规范'] else icon('arrow.left')
        right=icon('plus' if title=='消息' else ('gearshape' if title=='我' else 'ellipsis'))
        action=re.search(r'>\s*(发布|完成)\s*<',text)
        if action: right='<span class="nav-action">'+action[1]+'</span>'
        return '<header class="topbar"><span class="nav-icon">'+left+'</span><div class="nav-title"><h2>'+title+'</h2>'+('<small>'+small[1]+'</small>' if small else '')+'</div><span class="nav-icon">'+right+'</span></header>'
    s=re.sub(r'<header class="topbar">.*?</header>',header,s,flags=re.S)
    def nav(m):
        a=re.search(r'class="tab active"[^>]*>(.*?)</div>',m[0],re.S)
        label=re.sub('<[^>]+>','',a[1]) if a else '消息'
        active=next((x for x in ['消息','通讯录','发现','我'] if x in label),'消息')
        return tabs(active)
    s=re.sub(r'<nav class="tabs".*?</nav>',nav,s,flags=re.S)
    # Every screen includes the same four-tab navigation, including flow steps.
    sections=s.split('<section class="phone"')
    for i in range(1,len(sections)):
        phone,tail=sections[i].split('</section>',1)
        if 'class="tabs"' not in phone:
            phone=phone.replace('<div class="home">',tabs('消息')+'<div class="home">')
        sections[i]=phone+'</section>'+tail
    s='<section class="phone"'.join(sections)
    s=re.sub(r'<span\s+data-icon="([^"]+)"\s*></span>',lambda m:icon(m[1]),s)
    s=s.replace('低保真静态示意','v2 静态设计稿')
    path.write_text(s)

if __name__=='__main__':
    for p in sorted(ROOT.glob('*.html')):
        apply(p)
    print('已内联统一主题和自绘 SVG。')
