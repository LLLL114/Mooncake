#!/usr/bin/env python3

import sys as _sys
_sys.dont_write_bytecode = True
import pathlib,tempfile,unittest
from metrics import DTYPE,np,q,summarize
class MetricsTest(unittest.TestCase):
 def test_nearest_rank(self):
  self.assertEqual(q([4,1,3,2],.5),2);self.assertIsNone(q([], .99))
 def test_arrival_cohorts_keep_late_completions(self):
  a=np.zeros(5000,dtype=DTYPE);a['planned']=np.arange(5000,dtype='uint64')*250000
  a['submitted']=a['planned']+1000;a['finished']=a['planned']+100000
  for k in range(5):a['finished'][k*1000+989:(k+1)*1000]=a['planned'][k*1000+989:(k+1)*1000]+1000000
  a['allocation_ns']=600;a['weight0']=.5;a['share0']=.5
  size=65536;c={'measurement_start_ns':250000000,'measurement_stop_ns':1250000000,'request_bytes':size,'arrival_interval_ns':250000,'kind':'fixture','topology':'equal','seed':1}
  bins=np.bincount((a['finished']//50000000).astype(int),minlength=26)[:25]*size
  assigned=np.bincount((a['planned']//50000000).astype(int),minlength=25)*size
  d={'config':c,'requests_completed':5000,'done_50ms':[[int(x/2),int(x/2)] for x in bins],'assigned_50ms':[[int(x/2),int(x/2)] for x in assigned],'weight_changes':{'samples':4000,'tv':0,'events':[0,0,0],'reversals':[0,0,0]},'aggregate_path':False,'split_count':1,'max_injection_lateness_ns':1000,'final_stats':[{'inflight':0},{'inflight':0}]}
  with tempfile.TemporaryDirectory() as temp:
   p=pathlib.Path(temp)/'trace.bin';a.tofile(p);row,w=summarize(d,p,{'1':100.,'2':100.,'3':200.})
  primary=[x for x in w if x['width_ns']==250000000]
  self.assertEqual(row['requests_measured'],4000);self.assertEqual(row['p99_us'],1000)
  self.assertEqual([x['p99_us'] for x in primary],[1000]*4)
  self.assertEqual(row['allocation_mean_ns'],600);self.assertEqual(row['p99_windows']['cv'],0)
  self.assertTrue(all(x['p99_us'] is None for x in w if x['width_ns']==50000000))
  self.assertIsNone(row['retry_migration']);self.assertEqual(row['pending_at_end'],0)
if __name__=='__main__':unittest.main()
