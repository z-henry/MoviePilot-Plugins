"""Read-only, in-memory probe against the deployed V2 parsing libraries.

Run with: python3 scripts/episodeguard_native_probe.py [--sudo]
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
               for name in ("__init__.py", "policy.py", "metadata.py", "overrides.py", "snapshots.py")}
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
mod('app.schemas.types', MediaType=MediaType,
    EventType=NS(DownloadAdded='added',TransferFailed='failed'), SystemConfigKey=NS(CustomIdentifiers='words'),
    ChainEventType=NS(ResourceDownload='download', ResourceSelection='selection',TransferIntercept='intercept'))
mod('app.core.event', Event=object, eventmanager=NS(register=lambda *a, **k: lambda f: f))
mod('app.log', logger=logger)
mod('app.core.config', settings=NS(RMT_MEDIAEXT=['.mkv','.mp4'], RMT_SUBEXT=['.srt','.ass']))

# Execute the actual V2 word matcher with only DB/config/log dependencies removed.
tree = ast.parse(Path('/app/app/core/meta/words.py').read_text())
tree.body = [n for n in tree.body if not (isinstance(n,ast.ImportFrom) and n.module and n.module.startswith('app.'))]
for n in tree.body:
    if isinstance(n, ast.ClassDef): n.keywords=[]
ns={'logger':logger, 'SystemConfigKey':NS(CustomIdentifiers='words')}
exec(compile(tree,'native-words','exec'),ns)
words=object.__new__(ns['WordsMatcher']); words.systemconfig=NS(get=lambda key: [])

# Exercise MP's actual formatting properties; equal endpoints have the same
# episode set as a scalar but produce different filenames/history strings.
format_tree=ast.parse(Path('/app/app/core/meta/metabase.py').read_text())
format_class=next(n for n in format_tree.body if isinstance(n,ast.ClassDef) and n.name=='MetaBase')
format_methods=[n for n in format_class.body if isinstance(n,ast.FunctionDef)
                and n.name in {'season','episode','season_episode'}]
format_node=ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(
    name='NativeFormat',bases=[],keywords=[],body=format_methods,decorator_list=[])],type_ignores=[]))
format_env={'MediaType':MediaType}
exec(compile(format_node,'native-episode-format','exec'),format_env)
class ParsedMeta(NS,format_env['NativeFormat']): pass

def native_meta(path, custom_words=None):
    # Preprocess each path component using host WordsMatcher, then use the
    # deployed native path parser. No live settings or database are imported.
    components=[words.prepare(part, custom_words)[0] for part in path.parts[1:]]
    p='/'+ '/'.join(components)
    options={'media_exts':['.mkv','.mp4','.srt','.ass'],'custom_words':custom_words or []}
    d=moviepilot_rust.parse_metainfo_path_fast(str(path),options)
    python_words=moviepilot_rust.parse_metainfo_path_fast(p,dict(options,custom_words=[]))
    for key in ('type','begin_season','end_season','begin_episode','end_episode','resource_pix'):
        assert d.get(key)==python_words.get(key), ('Rust/Python word matcher difference',path,key,d,python_words)
    s=d.get('begin_season'); e=d.get('begin_episode'); end=d.get('end_episode')
    return ParsedMeta(type=MediaType.TV if d.get('type')=='电视剧' else MediaType.MOVIE,
              begin_season=s,end_season=d.get('end_season'),begin_episode=e,end_episode=end,
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
def native_download(torrent):
    state['download_calls']=state.get('download_calls',0)+1
    return state['content'],'',[]
mod('app.chain.download', DownloadChain=lambda: NS(download_torrent=native_download))
mod('app.chain.subscribe', SubscribeChain=lambda: NS(get_subscribe_by_source=lambda origin:
    NS(season=1,start_episode=1,total_episode=60,custom_words=state.get('words'))))
package=mod('episodeguard')
policy=mod('episodeguard.policy')
exec(sources['policy.py'],policy.__dict__)
metadata=mod('episodeguard.metadata')
exec(sources['metadata.py'],metadata.__dict__)
overrides=mod('episodeguard.overrides')
overrides.__package__='episodeguard'
exec(sources['overrides.py'],overrides.__dict__)
snapshots=mod('episodeguard.snapshots')
snapshots.__package__='episodeguard'
exec(sources['snapshots.py'],snapshots.__dict__)
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
# Only an isolated in-memory SQLite DB; never import or connect to MP's DB.
from sqlalchemy import Column,Integer,String,JSON,create_engine,select
from sqlalchemy.orm import declarative_base,Session
BaseORM=declarative_base()
class History(BaseORM):
    __tablename__='native_probe_history'
    id=Column(Integer,primary_key=True)
    download_hash=Column(String)
    media_source=Column(String)
    media_id=Column(String)
    seasons=Column(String)
    episodes=Column(String)
    custom_words=Column(String)
    note=Column(JSON)
    date=Column(String)
engine=create_engine('sqlite://')
BaseORM.metadata.create_all(engine)
def rows(hashes):
    with engine.connect() as connection:
        return [dict(r) for r in connection.execute(select(History.__table__).where(
            History.download_hash.in_(hashes))).mappings()]
mod('app.core.meta.words',WordsMatcher=lambda: words)
mod('app.db.systemconfig_oper',SystemConfigOper=lambda: NS(get=lambda key: []))
mod('app.utils.media',resolve_media_identity=lambda **kwargs: ('themoviedb','220542'))
force_cases=[
 ('inherit-conflict', [cases[0][1][0]], None, ''),
 ('inherit-parent-season', ['Season 3/'+cases[0][1][0]], None, 'Show'),
 ('inherit-existing-offset', [cases[0][1][0]], rule, ''),
 ('inherit-unknown-episode', ['The.Apothecary.Diaries.1080p.mkv'], None, ''),
 ('inherit-subtitle', [cases[0][1][0],cases[0][1][0].replace('.mkv','.chs.srt')], None, 'Show'),
 ('inherit-brackets-unicode', ['[字幕组] 药屋少女的呢喃 - 02 [1080p].mkv'], None, ''),
 ('inherit-blue-box-26', ['Blue.Box.S02E26.1080p.NF.WEB-DL.DDP5.1.H.264-MWeb.mkv'], 'Blue Box S02 => Blue Box S01', '', 26),
 ('inherit-blue-box-27', ['Blue.Box.S02E27.1080p.NF.WEB-DL.DDP5.1.H.264-MWeb.mkv'], 'Blue Box S02 => Blue Box S01', 'Blue.Box.S02.1080p.NF.WEB-DL.DDP5.1.H.264-MWeb', 27),
 ('inherit-fx-03', ['[ANi] FX 戰士久留美 - 03 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4'], None, '', 3),
 ('inherit-normalized-equal-end', ['Show.S03E01-E50.mkv'], None, ''),
]
for index,case in enumerate(force_cases):
    label,names,custom,folder=case[:4]
    episode=case[4] if len(case)>4 else 50
    state.update(content=content(names,folder), words=custom)
    instance=package.EpisodeGuard(); instance.init_plugin({'enabled':True})
    instance._mode='inherit'
    instance._snapshots=snapshots.TaskSnapshots([],lambda value: None)
    instance._snapshots.start(History,rows)
    expected=NS(begin_season=1,end_season=None,begin_episode=episode,end_episode=None,season_list=[1],episode_list=[episode])
    data=NS(cancel=False,source='',reason='',origin='Subscribe|{}',episodes={episode},
        context=NS(meta_info=expected,media_info=NS(type=MediaType.TV),
                   torrent_info=NS(title=label,site_name='offline',enclosure='https://example.invalid/test.torrent')))
    instance.on_resource_download(NS(event_data=data))
    assert not data.cancel, (label,data.reason)
    value=overrides.torrent_hashes(state['content'])[0]
    with Session(engine) as session:
        row=History(download_hash=value,media_source='themoviedb',media_id='220542',seasons='S01',episodes=f'E{episode:02d}',
                    custom_words=custom,note={'source':data.origin},date=str(index))
        session.add(row); session.commit()
    saved=instance._snapshots.ready([value])
    assert saved and saved['custom_words'], label
    fileitem=NS(storage='local',path='/download/'+(folder+'/' if folder else '')+names[0])
    actual=native_meta(Path(fileitem.path),saved['custom_words'].splitlines())
    assert actual.season_list==[1] and actual.episode_list==[episode], (label,actual)
    assert actual.end_season is None and actual.end_episode is None,(label,actual)
    assert actual.season=='S01' and actual.episode==f'E{episode:02d}',(label,actual.season,actual.episode)
    assert actual.season_episode==f'S01 E{episode:02d}',(label,actual.season_episode)
    instance._file_tasks=lambda fileitem: [NS(download_hash=value)]
    check=NS(cancel=False,source='',reason='',fileitem=fileitem,meta=actual,mediainfo=data.context.media_info)
    instance.on_transfer_intercept(NS(event_data=check))
    assert not check.cancel,(label,check.reason)
    instance.stop_service()
    print(label,f'S01E{episode:02d}, native format and committed snapshot PASS')

pack_cases=[
 ('pack-cyberpunk-season', [f'Cyberpunk.Edgerunners.S02E{i:02d}.1080p.mkv' for i in range(1,11)],2,list(range(1,11)),list(range(1,11)),None,0),
 ('pack-selected-subset', [f'Cyberpunk.Edgerunners.S02E{i:02d}.mkv' for i in range(1,11)],2,list(range(1,11)),[5],'S03 => S02',0),
 ('pack-late-files',['Show.S03E01.mkv','Show.S03E02.mkv'],1,[50],[50],None,1),
 ('pack-late-merged',['Show.S03E01-E02.mkv'],1,[50],[50],None,1),
 ('pack-unlabelled-season',['Show.S01E01.mkv','Show.S01E02.mkv'],1,[],[],None,0),
 ('pack-multiple-seasons',['Show.S01E01.mkv','Show.S02E01.mkv'],1,[1],[1],None,0),
]
for index,(label,names,season,declared,requested,custom,fetches) in enumerate(pack_cases):
    state.update(content=content(names,'Pack' if len(names)>1 else ''),words=custom,download_calls=0)
    instance=package.EpisodeGuard(); instance.init_plugin({'enabled':True})
    instance._mode='inherit'; instance._block_unverifiable=False
    instance._snapshots=snapshots.TaskSnapshots([],lambda value: None)
    instance._snapshots.start(History,rows)
    expected=NS(begin_season=season,end_season=None,
        begin_episode=min(declared) if declared else None,end_episode=max(declared) if len(declared)>1 else None,
        season_list=[season],episode_list=declared)
    if label=='pack-multiple-seasons': expected.end_season=2; expected.season_list=[1,2]
    data=NS(cancel=False,source='',reason='',origin='Subscribe|{}',episodes=set(requested),
        context=NS(meta_info=expected,media_info=NS(type=MediaType.TV),
                   torrent_info=NS(title=label,site_name='offline',enclosure='https://example.invalid/test.torrent')))
    instance.on_resource_download(NS(event_data=data))
    assert not data.cancel,(label,data.reason)
    assert state['download_calls']==fetches,(label,state['download_calls'])
    assert data.episodes==set(requested) and expected.episode_list==declared,label
    assert instance._records[0]['result']=='放行（合集原生整理）',label
    value=overrides.torrent_hashes(state['content'])[0]
    assert not instance._snapshots.matching_hash(value),label
    with Session(engine) as session:
        row=History(download_hash=value,media_source='themoviedb',media_id='220542',seasons=f'S{season:02d}',
                    episodes='E01-E10',custom_words=custom,note={'source':data.origin},date=f'pack-{index}')
        session.add(row); session.flush()
        assert row.custom_words==custom and overrides.MARKER not in row.note,label
        session.rollback()
    instance.stop_service(); print(label,'native PASS, no plugin snapshot')
engine.dispose()
print('native probe: 24 cases passed; no network, live database or downloader writes')
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
