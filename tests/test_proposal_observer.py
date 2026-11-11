import hashlib,json,os,sys,time
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'eval'),str(ROOT/'src'),str(ROOT/'eval/tools')]
import entry_metrics as em
from hard_deadline import Collector
from seereval.schema import from_ddl
from seereval import validator,tools

@pytest.fixture
def logs(tmp_path):
    if em._fd is not None:os.close(em._fd)
    em._fd=None;em.configure(tmp_path/'entries.jsonl','test')
    yield tmp_path
    os.close(em._fd);em._fd=None

def entries(root):
    return em.read_entries(root/'entries.jsonl',0,time.monotonic()+1)['entries']

def test_request_fingerprint_allowlist_never_persists_sql_or_credentials(tmp_path):
    req={'q1':"SELECT 'secret-sql'",'q2':'SELECT 2','mutants':['SELECT 3','SELECT 3'],
         'ddl':'CREATE TABLE private_table(x INT)','url':'mysql://user:TOP_SECRET@host/db',
         'api_key':'KEY_SECRET','timeout':2,'seed':0,'unknown':{'password':'NESTED_SECRET'}}
    bind=em.bind_request('parseval',req,tmp_path,'one')
    data=(tmp_path/bind['request_file']).read_text()
    assert all(x not in data for x in ['secret-sql','private_table','TOP_SECRET','KEY_SECRET','NESTED_SECRET','mysql://'])
    body=json.loads(data);assert body['query_sha256']['q1']==[hashlib.sha256(req['q1'].encode()).hexdigest()]
    assert len(body['query_sha256']['mutants'])==2
    assert body['query_sha256']['mutants'][0]==body['query_sha256']['mutants'][1]

def test_native_call_return_and_request_result_tamper_rejected(logs,monkeypatch):
    obj={'verdict':'neq','instance':{'t':[{'x':1}]}}
    seen=[]
    def native(tool,req,hard_s,mem_gb=None):
        seen.append((tool,req,hard_s));return obj
    monkeypatch.setattr(tools,'call',native)
    em.instrument_generation(time.monotonic()+20,logs)
    req={'q1':'SELECT 1','q2':'SELECT 2','timeout':2,'url':'secret'}
    assert tools.call('parseval',req,3) is obj
    assert seen[0][1]['q1']==req['q1'] and seen[0][1]['url']=='secret'
    es=entries(logs);rows=em.verify_generation_observations(logs,es)
    assert rows[0]['state']=='completed_return' and rows[0]['request_observed']
    bound=next(e for e in es if e['kind']=='generation_request_bound')
    path=logs/bound['request_file'];old=path.read_bytes();path.write_bytes(old+b' ')
    with pytest.raises(AssertionError,match='request identity changed'):em.verify_generation_observations(logs,es)
    path.write_bytes(old)
    finish=next(e for e in es if e['kind']=='generation_api_finished')
    path=logs/finish['result_file'];path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(AssertionError,match='native result changed'):em.verify_generation_observations(logs,es)

def test_native_exception_propagates_and_is_not_a_completed_return(logs,monkeypatch):
    error=RuntimeError('PRIVATE_ERROR')
    def native(*a,**kw):raise error
    monkeypatch.setattr(tools,'call',native);em.instrument_generation(time.monotonic()+20,logs)
    with pytest.raises(RuntimeError) as got:tools.call('parseval',{'q1':'x','q2':'y','timeout':1},1)
    assert got.value is error
    assert em.verify_generation_observations(logs,entries(logs))[0]['state']=='exception'
    assert 'PRIVATE_ERROR' not in (logs/'entries.jsonl').read_text()

def test_start_without_return_is_unfinished_not_unsupported(logs):
    em.record('generation_api_start',tool='parseval',attempt_id='k',n_queries=2)
    row=em.verify_generation_observations(logs,entries(logs))[0]
    assert row['state']=='unfinished' and row['finished_in_window'] is None

class Conn:
    def __init__(self):self.notices=[]
    def send(self,v):self.notices.append(v)

def collector(root):
    schema=from_ddl('trace','CREATE TABLE t(x INTEGER PRIMARY KEY NOT NULL);',dialect='sqlite')
    (root/'schema.json').write_text(json.dumps(schema.to_json()))
    batch={'schema_path':'schema.json','dialect':'sqlite','queries':[{'qid':'all','sql':'SELECT x FROM t'},{'qid':'none','sql':'SELECT x FROM t WHERE 0'}]}
    em.install_validation_observer()
    return Collector(batch,str(root),time.monotonic()+20,Conn(),root)

def test_real_sqlite_valid_and_constraint_rejected_offers_observed(logs):
    c=collector(logs);c.offer({'t':[{'x':1}]});c.offer({'t':[{'x':None}]})
    es=entries(logs);checks=[e for e in es if e['kind']=='common_validation_finished']
    assert [e['status'] for e in checks]==['valid','valid','violating']
    assert [e['check_index'] for e in checks]==[1,2,1]
    assert c.offers==2 and c.checks==3 and len(c.accepted)==1
    assert len([e for e in es if e['kind']=='common_offer_returned'])==2

def test_load_error_and_exception_kept_distinct(logs,monkeypatch):
    def load_error(*a,**kw):return {'status':'load_error','violations':[],'load_error':'PRIVATE_VALUE'}
    monkeypatch.setattr(validator,'check_db',load_error);em.install_validation_observer()
    with em.observe_common_offer(1,{}):assert validator.check_db()['status']=='load_error'
    error=ValueError('PRIVATE_EXCEPTION')
    def throws(*a,**kw):raise error
    monkeypatch.setattr(validator,'check_db',throws);em.install_validation_observer()
    with pytest.raises(ValueError) as got:
        with em.observe_common_offer(2,{}):validator.check_db()
    assert got.value is error
    es=entries(logs);assert any(e['kind']=='common_validation_finished' and e['status']=='load_error' for e in es)
    assert any(e['kind']=='common_offer_exception' for e in es)
    assert all(x not in (logs/'entries.jsonl').read_text() for x in ['PRIVATE_VALUE','PRIVATE_EXCEPTION'])

def test_expired_offer_cannot_create_certificate(logs):
    c=collector(logs);c.deadline=time.monotonic()-1;c.offer({'t':[{'x':1}]})
    assert not c.accepted and c.checks==0
    es=entries(logs);assert not any(e['kind']=='common_validation_finished' for e in es)

def test_offer_observation_identity_is_independent_of_early_return(logs):
    c=collector(logs);c.deadline=time.monotonic()-1
    c.offer({'t':[{'x':1}]});c.offer({'t':[{'x':2}]})
    es=entries(logs)
    assert [e['offer_id'] for e in es if e['kind']=='common_offer_attempt']==[1,2]
    assert c.offers==0 and c.checks==0 and not c.accepted
