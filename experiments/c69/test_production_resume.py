import unittest
from production_resume import post_admission,partition_work,GIB
class ResumeTests(unittest.TestCase):
 def test_replay_reserves_capacity_before_bird(self):
  # The observed 178 GiB / 193 GiB state must block new 8 GiB BIRD work.
  ready={'replay':True,'xdata_table':False,'formal_tables':False,'bird_table':False}
  selected,blocked=post_admission(ready,{},178*GIB,193*GIB,10)
  self.assertEqual(selected,[]);self.assertTrue(blocked)
  selected,blocked=post_admission(ready,{},170*GIB,193*GIB,11)
  self.assertEqual(selected,[('replay',18*GIB)]);self.assertFalse(blocked)
 def test_slot_and_followup_priority(self):
  ready={'replay':True,'xdata_table':True,'formal_tables':True}
  selected,blocked=post_admission(ready,{'replay':0},0,64*GIB,1)
  self.assertEqual(selected,[('xdata_table',4*GIB)]);self.assertTrue(blocked)
  self.assertEqual(post_admission(ready,{'replay':0,'xdata_table':'running'},0,64*GIB,1),([('formal_tables',4*GIB)],False))
 def test_resume_skips_completed_and_adopts_live(self):
  self.assertEqual(partition_work(5,{0,1,2},{2,3},{0,1,2,3}),([0,1],[2,3],[4]))
  with self.assertRaises(AssertionError):partition_work(5,{0},{2},{0,1,2})
if __name__=='__main__':unittest.main()

class ResourceTests(unittest.TestCase):
 def test_only_rlimit_parameter_changes(self):
  from group_resources import ResourceSubprocess
  from unittest.mock import Mock
  delegate=Mock();delegate.call.return_value=0
  proxy=ResourceSubprocess(delegate,2*GIB,8*GIB)
  args=['prlimit',f'--as={8*GIB}','--','/usr/bin/time','-v','timeout','840','python','method.py','--budget','600']
  proxy.call(args,cwd='/same',env={'same':'value'})
  delegate.call.assert_called_once_with([args[0],f'--as={2*GIB}',*args[2:]],cwd='/same',env={'same':'value'})
  self.assertEqual(args[1],f'--as={8*GIB}');self.assertEqual(proxy.calls,1)
