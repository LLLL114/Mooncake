#!/usr/bin/env python3
"""Check strict weak ordering using the comparator extracted from pinned source."""

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT, REPO_ROOT
import pathlib,subprocess,json,hashlib
ROOT=pathlib.Path(__file__).resolve().parents[1];repo=REPO_ROOT;tent=repo/'mooncake-transfer-engine/tent';b=OUTPUT_ROOT/'build/release';out=OUTPUT_ROOT/'runs/experiment-ef/comparator';out.mkdir(parents=True,exist_ok=True)
q=(tent/'src/transport/rdma/quota.cpp').read_text();start=q.index('[this](const Candidate& a, const Candidate& b)');end=q.index('});',start)+1;original=q[start:end]
assert 'std::abs(a.score - b.score)' in original and 'return a.dev_id < b.dev_id;' in original
code='#include "tent/transport/rdma/quota.h"\n#include <iostream>\nint main(){using Candidate=mooncake::tent::DeviceSelector::Candidate; mooncake::tent::DeviceSelector::SchedulingParams params;params.score_jitter_range=1e-9;auto less='+original.replace('[this]','[&]').replace('sched_params_.','params.')+';Candidate a{0,10e-6+1.5e-9,false},b{1,10e-6+.75e-9,false},c{2,10e-6,false};std::cout<<less(a,b)<<" "<<less(b,c)<<" "<<less(a,c)<<"\\n";}\n'
(b/'comparator_check.cpp').write_text(code);subprocess.run(['g++','-std=c++17','-O2','-I'+str(tent/'include'),str(b/'comparator_check.cpp'),'-o',str(b/'comparator_check')],check=True)
values=[int(x) for x in subprocess.check_output([str(b/'comparator_check')],text=True).split()];assert values==[1,1,0]
result={'a_less_b':True,'b_less_c':True,'a_less_c':False,'strict_weak_order_violation':True,'original_comparator_sha256':hashlib.sha256(original.encode()).hexdigest(),'scope':'three constructed candidate scores, exact production comparator; no claim measured multi-rail traces reached these scores; two-candidate main results do not exercise a three-candidate cycle'}
(out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print('COMPARATOR_RESULT',result,flush=True)
