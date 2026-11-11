import unittest
from seereval.schema import from_ddl
from seereval.validator import check_db

DDL='''CREATE TABLE p(a INT, b INT, code TEXT UNIQUE,
CONSTRAINT pk PRIMARY KEY(a,b));
CREATE TABLE c(id INT UNIQUE, a INT, b INT,
CONSTRAINT fk FOREIGN KEY(a,b) REFERENCES p(a,b),
CONSTRAINT uq UNIQUE(a,b));'''

class InputConstraintTest(unittest.TestCase):
 def setUp(self): self.s=from_ddl('test',DDL,dialect='sqlite')
 def test_named_key_and_inline_unique(self):
  self.assertEqual(self.s.table('p').primary_key,['a','b'])
  self.assertIn(['code'],self.s.table('p').unique)
  data={'p':[{'a':1,'b':2,'code':'x'},{'a':1,'b':3,'code':'x'}]}
  self.assertEqual(check_db(self.s,'sqlite',data,{},also_bare=False)['status'],'violating')
 def test_named_unique_and_composite_foreign_key(self):
  self.assertIn(['a','b'],self.s.table('c').unique)
  good={'p':[{'a':1,'b':2,'code':'x'}],'c':[{'id':1,'a':1,'b':2}]}
  bad={'p':good['p'],'c':[{'id':1,'a':1,'b':3}]}
  self.assertEqual(check_db(self.s,'sqlite',good,{},also_bare=False)['status'],'valid')
  self.assertEqual(check_db(self.s,'sqlite',bad,{},also_bare=False)['status'],'violating')
 def test_inline_reference(self):
  s=from_ddl('inline','CREATE TABLE p(id INT PRIMARY KEY); CREATE TABLE c(id INT REFERENCES p(id));',dialect='sqlite')
  self.assertEqual(s.table('c').foreign_keys,[{'columns':['id'],'ref_table':'p','ref_columns':['id']}])

if __name__=='__main__':unittest.main()
