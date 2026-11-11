import hashlib,json,sys
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'eval')]
from seereval.schema import Schema
import seereval.tools as tools

def setup(tmp_path,monkeypatch):
 schema=Schema.from_json({'name':'fixture','tables':[{'name':'t','columns':[{'name':'x','type':'int','nullable':True}]}]})
 key=hashlib.sha256(json.dumps(schema.to_json(),sort_keys=True).encode()).hexdigest()
 base=tmp_path/'sqldriller/resources/declared';(base/'index').mkdir(parents=True);(base/'cubes-index-v3').mkdir()
 (base/'index'/(key+'.json')).write_text(json.dumps({'resource_view':'original-sqlite.json'}))
 monkeypatch.setattr(tools,'ROOT',str(tmp_path))
 return schema,base,key

def test_mysql_registry_is_separate_and_queries_remain_original(tmp_path,monkeypatch):
 schema,base,key=setup(tmp_path,monkeypatch)
 (base/'cubes-index-v3'/(key+'.json')).write_text(json.dumps({'resource_view':'bridge-release-cache.json'}))
 sqls=['SELECT x FROM t','SELECT x FROM t LIMIT 1']
 mysql=tools.build_request('sqldriller_official',schema,'mysql',sqls,20,0)
 sqlite=tools.build_request('sqldriller_official',schema,'sqlite',sqls,20,0)
 assert mysql['resource_view']=='bridge-release-cache.json' and sqlite['resource_view']=='original-sqlite.json'
 assert mysql['dialect']=='mysql' and [mysql['q1'],mysql['q2']]==sqls

def test_missing_mysql_registry_does_not_fall_back_to_sqlite_view(tmp_path,monkeypatch):
 schema,base,key=setup(tmp_path,monkeypatch)
 req=tools.build_request('sqldriller_official',schema,'mysql',['SELECT x FROM t']*2,20,0)
 assert 'resource_view' not in req and req['require_declared_schema']
