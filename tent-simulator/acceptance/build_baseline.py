#!/usr/bin/env python3
"""Generate a ring-buffer real harness; original selector source is unchanged."""

import sys as _sys
_sys.dont_write_bytecode = True
from pathlib import Path as _BootstrapPath
_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[1] / "scripts"))
from experiment_paths import OUTPUT_ROOT, REPO_ROOT, ACCEPTANCE_OUTPUT, artifact_key
import argparse, hashlib, json, pathlib, subprocess
HERE=pathlib.Path(__file__).resolve().parent
ROOT=HERE.parent
B=OUTPUT_ROOT/'build/release'
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def generate():
    shared=(ROOT/'simulator/driver_d.cpp').read_text().split('int main(int argc, char** argv)')[0]
    shared='#include "acceptance_metrics.h"\n'+shared
    anchor='    if (d_request != current_request) {'
    assert shared.count(anchor)==1
    shared=shared.replace(anchor,'    acceptance::decision(now_ns, d_last[0]);\n'+anchor)
    (B/'acceptance_shared.inc').write_text(shared)
    s=(ROOT/'simulator/driver_real.cpp').read_text()
    def sub(a,b):
        nonlocal s
        assert s.count(a)==1,(a,s.count(a));s=s.replace(a,b)
    sub('#include "driver_d_shared.inc"','#include "acceptance_shared.inc"')
    sub('n<=50000','n<=8000000')
    sub('  std::vector<std::vector<int>> routes(n);std::vector<std::vector<uint64_t>> charges(n),post_ns(n,std::vector<uint64_t>(count));',
        '  std::vector<std::vector<int>> routes(slots);std::vector<std::vector<uint64_t>> charges(slots),post_ns(slots,std::vector<uint64_t>(count));')
    sub('  std::vector<uint64_t> remaining(n,count),submitted(n),finished(n),planned(n);',
        '''  std::vector<uint64_t> remaining(slots,count);
  std::vector<acceptance::Record> records(n);
  const uint64_t measure_start=config.value("measurement_start_ns",1000000000ULL);
  const uint64_t measure_stop=config.value("measurement_stop_ns",11000000000ULL);
  const bool fixed_count=config.value("fixed_count",false);
  acceptance::Collector collector(measure_start,measure_stop,config.value("metric_weights",true));
  acceptance::active=&collector;
  bool closed=false;
  uint64_t allocation_calls=0;''')
    for a,b in [('routes[req]','routes[req%slots]'),('charges[req]','charges[req%slots]'),('post_ns[req]','post_ns[req%slots]'),('remaining[req]','remaining[req%slots]'),('submitted[req]','records[req].submitted'),('planned[req]','records[req].planned'),('finished[req]','records[req].finished')]:s=s.replace(a,b)
    sub('  uint64_t next=0,done=0,wc_count=0,begin=clockNs(),end=0,max_late=0;double allocation_tv=0,last_share=0;', '  std::array<uint64_t,64> active_request{}, completed_mask{};\n  uint64_t next=0,done=0,wc_count=0,begin=clockNs(),end=0,max_late=0;double allocation_tv=0,last_share=0;')
    sub('req<next && s<count && routes[req%slots][s]==rail', 'req<next && s<count && active_request[req%slots]==req && !(completed_mask[req%slots] & (1ULL<<s)) && routes[req%slots][s]==rail')
    sub('     --posted[rail];', '     completed_mask[req%slots]|=1ULL<<s;\n     --posted[rail];')
    sub('  while(done<n) {','  while(done<next || (!closed && next<n)) {')
    sub('now-begin<30000000000ULL','now-begin<120000000000ULL')
    sub('++wc_count;done_bytes[rail]+=split.bytes;',
        '++wc_count;done_bytes[rail]+=split.bytes;collector.bytes(polltime-begin,rail,split.bytes,true);')
    sub('   if(next<n && !busy[next%slots] && (!gap || now-begin>=next*gap)) {',
        '''   if(!gap && !fixed_count && now-begin>=measure_stop)closed=true;
   if(next<n && !closed && !busy[next%slots] && (!gap || now-begin>=next*gap)) {''')
    sub('    experiment::now_ns=now-begin;current_request=req;',
        '''    experiment::now_ns=now-begin;current_request=req;
    routes[req%slots].clear();charges[req%slots].clear();remaining[req%slots]=count;active_request[req%slots]=req;completed_mask[req%slots]=0;
    uint64_t alloc_begin=clockNs();''')
    sub('    double share=',
        '''    records[req].allocation_ns=clockNs()-alloc_begin;
    allocation_calls+=split.aggregate?1:count;
    records[req].first_weight0=d_first[0];records[req].first_rail=routes[req%slots][0];
    double share=''')
    sub('/count;if(req>=256)', '/count;records[req].share0=share;if(req>=256)')
    sub('    for(uint64_t s=0;s<count;++s)waiting[routes[req%slots][s]].push_back({req,s});',
        '    for(uint64_t s=0;s<count;++s){waiting[routes[req%slots][s]].push_back({req,s});collector.bytes(now-begin,routes[req%slots][s],split.bytes,false);}')
    sub('wc_count==n*count && done_bytes[0]+done_bytes[1]==n*length',
        'wc_count==next*count && done_bytes[0]+done_bytes[1]==next*length')
    sub('  std::vector<uint64_t> scheduled(n),service(n);for(uint64_t r=0;r<n;++r){scheduled[r]=finished[r]-planned[r];service[r]=finished[r]-submitted[r];}',
        '''  require(fixed_count || gap || closed,"saturated request storage exhausted before measurement end");
  records.resize(next);
  auto raw_path=std::string(argv[2])+".requests.bin";
  std::ofstream raw(raw_path,std::ios::binary);
  raw.write(reinterpret_cast<const char*>(records.data()),records.size()*sizeof(acceptance::Record));
  require(bool(raw),"raw trace write failed");raw.close();
  auto changes=[](const acceptance::Changes& c)->Json{return {{"samples",c.samples},{"tv",c.tv},{"events",c.events},{"reversals",c.reversals}};};
  Json weight_windows=Json::array();for(const auto& w:collector.windows)weight_windows.push_back(changes(w));''')
    sub('{"request_scheduled_latency_ns",scheduled},{"request_service_latency_ns",service},{"submitted_ns",submitted},{"finished_ns",finished},',
        '{"requests_completed",next},{"split_count",count},{"aggregate_path",split.aggregate},{"allocation_calls",allocation_calls},{"raw_trace",raw_path},{"raw_format","little-endian QQQQddQ, planned/submitted/finished/allocate ns, first_weight0/share0, first_rail"},{"weight_changes",changes(collector.total)},{"weight_windows_250ms",weight_windows},{"done_50ms",collector.done},{"assigned_50ms",collector.assigned},')
    s=s.replace('allocation_tv/(n-257)', 'allocation_tv/std::max<uint64_t>(1,next-257)')
    (B/'acceptance_real.cpp').write_text(s)
    # Deterministic model observes the same candidates and exercises all old paths.
    wrapper=(ROOT/'simulator/driver_d.cpp').read_text()
    wrapper=wrapper.replace('#include "driver.cpp"','#include "driver_f_model.inc"')
    (B/'acceptance_sim.cpp').write_text(wrapper)
    return s

def main():
    a=argparse.ArgumentParser();a.add_argument('--emit-only',action='store_true');args=a.parse_args()
    generate()
    if args.emit_only:return
    base=json.loads((B/'manifest-real.json').read_text())['command']
    commands=[]
    for name,src in [('acceptance_real',B/'acceptance_real.cpp'),('acceptance_sim',B/'acceptance_sim.cpp')]:
        cmd=[str(src) if x==str(ROOT/'simulator/driver_real.cpp') else str(B/name) if x==str(B/'real_rdma') else x for x in base]+['-I'+str(HERE)]
        subprocess.run(cmd,check=True);commands.append(cmd)
    files=[HERE/'build_baseline.py',HERE/'acceptance_metrics.h',B/'acceptance_shared.inc',B/'acceptance_real.cpp',B/'acceptance_sim.cpp',B/'quota.d.cpp',B/'quota.observed.cpp',B/'driver_f_model.inc',ROOT/'simulator/driver_real.cpp',ROOT/'simulator/driver.cpp',ROOT/'simulator/driver_d.cpp']
    manifest={'commands':commands,'files':{artifact_key(p):digest(p) for p in files},'binaries':{n:digest(B/n) for n in ['acceptance_real','acceptance_sim','dynamic','concurrency','candidate_real']},'production_quota_sha256':digest(REPO_ROOT/'mooncake-transfer-engine/tent/src/transport/rdma/quota.cpp')}
    (ACCEPTANCE_OUTPUT/'build-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print('ACCEPTANCE_BUILD_OK',flush=True)
if __name__=='__main__':main()
