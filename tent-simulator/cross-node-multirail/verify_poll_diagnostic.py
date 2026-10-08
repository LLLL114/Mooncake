#!/usr/bin/env python3
"""Check the C++ fixture, clipping and draining semantics independently."""
import json
from pathlib import Path
import struct
import sys
from poll_diagnostic_metrics import COMP,GAP,timing,overlap_index,clock_offset

root=Path(sys.argv[1]); meta=json.loads((root/'poll-diagnostic.json').read_text())
assert meta['error']==0
assert clock_offset(meta)['uncertainty_ns']<=10000
row=COMP.unpack((root/'completions-0.bin').read_bytes())
assert row==(7,65536,65536,90,115,2000220,2000230,220,230,120,130,2000200,2000210,2000300,0,0,0,0)
assert GAP.unpack((root/'poll-gaps-0.bin').read_bytes())==(230,2000220,2000230,2000200,2000210,16,0,1)
assert meta['workers'][0]['polls'][0]['calls']==3 and meta['workers'][0]['polls'][0]['full']==1
s=dict(worker=0,dev=0,submit=2000000,poll_end=4000000,poll_begin=3999990,drained_begin=1999990,drained_end=2000000)
index=overlap_index({(0,0):[dict(previous_end=1,end=2500000)]})
assert timing(s,index)['gap_fraction']==.25 # idle prefix must not count against a posted Slice.
s.update(drained_begin=3999900,drained_end=3999910)
assert timing(s,{})['category']=='dense_polling'
assert timing(s,overlap_index({(0,0):[dict(previous_end=1,end=4000000)]}))['category']=='large_poll_service_gaps'
clock=dict(clock='CLOCK_REALTIME',clock_samples=[[1000,10000001005,1010],[2000,10000002005,2010]])
assert clock_offset(clock)['offset_ns']==10000000000
clock['clock_samples'][1][1]+=20000
try: clock_offset(clock)
except ValueError: pass
else: raise AssertionError('clock jump must invalidate cross-clock analysis')
print('POLL_DIAGNOSTIC_PY_TEST_OK')
