"""Read-only, in-memory probe against the deployed V2 parsing libraries.

Run with: python3 scripts/native_probe.py [--sudo]
No host application is imported, no torrent is fetched, no files are written in
the container, and python -B suppresses bytecode writes. App boundaries are fake.
"""

import argparse
import getpass
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sudo", action="store_true")
    parser.add_argument("--container", default="moviepilot-v2")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sources = {name: (root / "plugins.v2/episodeguard" / name).read_text()
               for name in ("__init__.py", "policy.py")}
    probe = r'''
import ast, json, sys, types
from enum import Enum
from pathlib import Path
from types import SimpleNamespace as NS
import moviepilot_rust
from torrentool.api import Torrent

class MediaType(Enum):
    TV = "电视剧"
    MOVIE = "电影"

class Base:
    def get_data(self, key): return []
    def save_data(self, key, value): pass

def mod(name, **attrs):
    m = types.ModuleType(name); m.__dict__.update(attrs); m.__path__ = []
    sys.modules[name] = m
    return m

logger = NS(info=lambda *a: None, debug=lambda *a: None, warn=lambda *a: None,
            error=lambda *a: None)
for name in ['app','app.chain','app.core','app.schemas','app.helper']: mod(name)
mod('app.plugins', _PluginBase=Base)
mod('app.schemas.types', MediaType=MediaType, ChainEventType=NS(ResourceDownload='download'))
mod('app.core.event', Event=object, eventmanager=NS(register=lambda *a, **k: lambda f: f))
mod('app.log', logger=logger)
mod('app.core.config', settings=NS(RMT_MEDIAEXT=['.mkv','.mp4']))

# Execute the actual V2 word matcher with only DB/config/log dependencies removed.
tree = ast.parse(Path('/app/app/core/meta/words.py').read_text())
tree.body = [n for n in tree.body if not (isinstance(n,ast.ImportFrom) and n.module and n.module.startswith('app.'))]
for n in tree.body:
    if isinstance(n, ast.ClassDef): n.keywords=[]
ns={'logger':logger, 'SystemConfigKey':NS(CustomIdentifiers='words')}
exec(compile(tree,'native-words','exec'),ns)
words=object.__new__(ns['WordsMatcher']); words.systemconfig=NS(get=lambda key: [])

def native_meta(path, custom_words=None):
    # Preprocess each path component using host WordsMatcher, then use the
    # deployed native path parser. No live settings or database are imported.
    components=[words.prepare(part, custom_words)[0] for part in path.parts[1:]]
    p='/'+ '/'.join(components)
    d=moviepilot_rust.parse_metainfo_path_fast(p, {'media_exts':['.mkv','.mp4'],'custom_words':[]})
    s=d.get('begin_season'); e=d.get('begin_episode'); end=d.get('end_episode')
    return NS(begin_season=s,end_season=d.get('end_season'),begin_episode=e,end_episode=end,
              season_list=[s if s is not None else 1] if d.get('type')=='电视剧' else [],
              episode_list=list(range(e,(end if end is not None else e)+1)) if e is not None else [])
mod('app.core.metainfo', MetaInfoPath=native_meta)

# Use the real V2 TorrentHelper parsing methods and torrentool on synthetic bytes.
t=ast.parse(Path('/app/app/helper/torrent.py').read_text())
methods=[n for c in t.body if isinstance(c,ast.ClassDef) and c.name=='TorrentHelper'
         for n in c.body if isinstance(n,ast.FunctionDef) and n.name in
         {'get_fileinfo_from_torrent','get_fileinfo_from_torrent_content'}]
node=ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),
    ast.ClassDef(name='NativeTorrentHelper',bases=[],keywords=[],body=methods,decorator_list=[])],type_ignores=[]))
env={'Torrent':Torrent,'Path':Path,'logger':logger,
     'StringUtils':NS(is_magnet_link=lambda x: isinstance(x,str) and x.startswith('magnet:'))}
exec(compile(node,'native-torrent-helper','exec'),env)
mod('app.helper.torrent', TorrentHelper=env['NativeTorrentHelper'])

state={}
mod('app.chain.download', DownloadChain=lambda: NS(download_torrent=lambda torrent: (state['content'],'',[])))
mod('app.chain.subscribe', SubscribeChain=lambda: NS(get_subscribe_by_source=lambda origin:
    NS(season=1,start_episode=1,total_episode=60,custom_words=state.get('words'))))
package=mod('episodeguard')
policy=mod('episodeguard.policy')
exec(sources['policy.py'],policy.__dict__)
exec(sources['__init__.py'],package.__dict__)

def bencode(value):
    if isinstance(value,int): return b'i'+str(value).encode()+b'e'
    if isinstance(value,str): value=value.encode()
    if isinstance(value,bytes): return str(len(value)).encode()+b':'+value
    if isinstance(value,list): return b'l'+b''.join(bencode(v) for v in value)+b'e'
    return b'd'+b''.join(bencode(k)+bencode(value[k]) for k in sorted(value))+b'e'

def content(names, folder=''):
    info={b'name':folder or names[0],b'piece length':16384,b'pieces':b'0'*20}
    if folder:
        info[b'files']=[{b'length':1,b'path':n.split('/')} for n in names]
    else: info[b'length']=1
    return bencode({b'info':info})

rule=r'The\.Apothecary\.Diaries\.S03E => The.Apothecary.Diaries.S01E && S01E <> (?<=S01E\d{2})\. >> EP+48'
cases=[
 ('user-conflict',['The.Apothecary.Diaries.S03E02.The.Fire-Rat.Cloak.1080p.NF.WEB-DL.AAC2.0.H.264-VARYG.mkv'],50,None,'',True),
 ('correct-single',['The.Apothecary.Diaries.S01E50.1080p.mkv'],50,None,'',False),
 ('offset-rule',['The.Apothecary.Diaries.S03E02.The.Fire-Rat.Cloak.1080p.NF.WEB-DL.AAC2.0.H.264-VARYG.mkv'],50,rule,'',False),
 ('episode-49-conflict',['The.Apothecary.Diaries.S03E01.Locusts.1080p.mkv'],49,None,'',True),
 ('episode-49-offset',['The.Apothecary.Diaries.S03E01.Locusts.1080p.mkv'],49,rule,'',False),
 ('wrong-episode',['The.Apothecary.Diaries.S01E51.1080p.mkv'],50,None,'',True),
 ('folder-season',['Season 1/The.Apothecary.Diaries.E50.mkv'],50,None,'The.Apothecary.Diaries',False),
 ('samples-and-subtitles',['The.Apothecary.Diaries.S01E50.mkv','Sample/sample.mkv','The.Apothecary.Diaries.S03E02.ass'],50,None,'Show',False),
]
for label, names, ep, custom, folder, blocked in cases:
    state.update(content=content(names,folder), words=custom)
    instance=package.EpisodeGuard(); instance.init_plugin({'enabled':True})
    expected=NS(begin_season=1,end_season=None,begin_episode=ep,end_episode=None,season_list=[1],episode_list=[ep])
    data=NS(cancel=False,source='',reason='',origin='Subscribe|{}',episodes={ep},
        context=NS(meta_info=expected,media_info=NS(type=MediaType.TV),
                   torrent_info=NS(title=label,site_name='offline',enclosure='https://example.invalid/test.torrent')))
    instance.on_resource_download(NS(event_data=data))
    assert data.cancel==blocked, (label,data.cancel,data.reason)
    print(label, 'BLOCK' if data.cancel else 'PASS')
print('native probe: 8 cases passed; no network, database or downloader writes')
'''
    code = "sources = " + repr(sources) + "\n" + probe
    password = None
    if args.sudo:
        password = getpass.getpass("sudo password: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\n")
    command = (["sudo", "-S", "-p", ""] if args.sudo else []) + [
        "docker", "exec", args.container, "python", "-B", "-c", code,
    ]
    result = subprocess.run(command, input=password + "\n" if password is not None else None,
                            text=True, capture_output=True, check=False)
    print(result.stdout, end="")
    if result.returncode:
        print(result.stderr, end="")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
