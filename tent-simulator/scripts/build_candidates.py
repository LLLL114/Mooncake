#!/usr/bin/env python3

import sys as _sys
_sys.dont_write_bytecode = True
from experiment_paths import OUTPUT_ROOT
import argparse,pathlib,json,subprocess,hashlib,difflib
ROOT=pathlib.Path(__file__).resolve().parents[1]
def main():
 p=argparse.ArgumentParser();p.add_argument('--sanitize',action='store_true');a=p.parse_args();b=OUTPUT_ROOT/'build'/('sanitize' if a.sanitize else 'release')
 base=json.loads((b/'manifest.json').read_text());command=next(c for c in base['commands'] if c[-1]==str(b/'observed'))
 quota=(b/'quota.d.cpp').read_text();original=quota
 quota='#include "policy_adapter.h"\n'+quota
 quota=quota.replace('    slice_dev_ids.clear();','    experiment::AllocationTimer allocation_timer;\n    slice_dev_ids.clear();')
 anchor='    if (num_slices == 1) {'
 assert quota.count(anchor)==1
 quota=quota.replace(anchor,'''    if (experiment::candidate_policy) {
        std::vector<experiment::mr::Candidate> view;
        for (const auto& c : tl_candidates) {
            int rank = getDeviceRank(location, c.dev_id);
            view.push_back({c.dev_id, c.score, theoreticalBandwidth(devices_[c.dev_id]),
                            sched_params_.numa_tier_weights[rank]});
        }
        std::vector<uint64_t> lengths;
        uint64_t offset = 0;
        for (uint32_t s = 0; s < num_slices; ++s) {
            uint64_t bytes = std::min(slice_bytes, total_length - offset);
            lengths.push_back(bytes); offset += bytes;
        }
        if (offset != total_length) return Status::InvalidArgument("slice geometry does not cover request");
        thread_local uint64_t calls = 0;
        bool probe = num_slices > 1 && (++calls % 100 == 0);
        auto assigned = experiment::candidateAllocate(this, view, lengths, probe,
                            std::hash<std::string>{}(location) ^ (uint64_t(priority) << 56));
        for (size_t s = 0; s < assigned.ids.size(); ++s) {
            int id = assigned.ids[s]; uint64_t bytes = assigned.bytes[s];
            devices_[id].addInflight(bytes);
            devices_[id].total_bytes.fetch_add(bytes, std::memory_order_relaxed);
            slice_dev_ids.push_back(id);
            if (slice_charged_bytes) slice_charged_bytes->push_back(bytes);
        }
        return Status::OK();
    }
'''+anchor)
 anchor='            if (std::abs(a.score - b.score) > sched_params_.score_jitter_range)'
 assert quota.count(anchor)==1
 quota=quota.replace(anchor,'''            if (experiment::candidate_policy)
                return a.score != b.score ? a.score < b.score : a.dev_id < b.dev_id;
'''+anchor)
 quota=quota.replace('if ((experiment::observe && experiment::wanted) || experiment::aggregate)', 'if (!experiment::candidate_policy && ((experiment::observe && experiment::wanted) || experiment::aggregate))')
 quota=quota.replace('    double observed_bw = static_cast<double>(length) / latency;', '    if (experiment::candidate_policy > 1) return Status::OK();\n    double observed_bw = static_cast<double>(length) / latency;')
 (b/'quota.candidates.cpp').write_text(quota);(b/'candidates.patch').write_text(''.join(difflib.unified_diff(original.splitlines(True),quota.splitlines(True),fromfile='quota.d.cpp',tofile='quota.candidates.cpp')))
 # Generated includes leave the validated historical drivers untouched.
 common=(b/'driver_f_model.inc').read_text()
 common='#include "policy_adapter.h"\n'+common.replace('    return sel;','    experiment::registerPolicy(sel.get(), config);\n    return sel;')
 common=common.replace('            s.post = now;', '            s.post = now;\n            experiment::policyPosted(sel.get(), rail, s.bytes, now);')
 common=common.replace('        ok(sel->release(s.rail, s.charged, (now-s.post)/1e9));','        experiment::policyCompleted(sel.get(), s.rail, s.charged, s.post, now);\n        ok(sel->release(s.rail, s.charged, (now-s.post)/1e9));')
 common=common.replace('return {{"sample_meters",experiment::samplingStats(rails)},','return {{"allocation_calls",experiment::allocation_calls.load()},{"allocation_ns",experiment::allocation_ns.load()},{"new_policy_stats",experiment::policyStats()},{"new_policy_calls",experiment::policy_calls.load()},{"new_policy_ns",experiment::policy_ns.load()},{"sample_meters",experiment::samplingStats(rails)},')
 (b/'candidate_model.inc').write_text(common)
 wrapper=(ROOT/'simulator/driver_d.cpp').read_text().replace('#include "driver.cpp"','#include "candidate_model.inc"\n#include "policy_metrics.inc"')
 (b/'candidate_sim.cpp').write_text(wrapper)
 shared=wrapper[:wrapper.index('int main(int argc, char** argv)')];(b/'candidate_shared.inc').write_text(shared)
 real=(ROOT/'simulator/driver_real.cpp').read_text().replace('#include "driver_d_shared.inc"','#include "candidate_shared.inc"')
 real=real.replace('     ok(sel->release(rail,charges[req][s],(polltime-post_ns[req][s])/1e9));','     experiment::policyCompleted(sel.get(), rail, charges[req][s], post_ns[req][s]-begin, polltime-begin);\n     ok(sel->release(rail,charges[req][s],(polltime-post_ns[req][s])/1e9));')
 anchor='++posted[rail];++writes[rail];'
 assert real.count(anchor)==1
 real=real.replace(anchor,'experiment::policyPosted(sel.get(),rail,split.bytes,post_ns[req][s]-begin);'+anchor)
 real=real.replace('Json trace=Json::array();std::vector<uint64_t> slice_completions;', 'Json trace=Json::array();std::vector<double> allocation_shares(n);')
 real=real.replace('/count;if(req>=256)', '/count;allocation_shares[req]=share;if(req>=256)')
 real=real.replace('{"trace",trace}', '{"allocation_shares",allocation_shares},{"trace",trace}')
 real=real.replace('{"weight0",d_first[0]}', '{"weight0",experiment::candidate_policy?previous_weights[0]:d_first[0]}')
 assert 'allocation_shares[req]=share' in real and 'policyCompleted(sel.get(), rail' in real
 real=real.replace('Json result={{"config",config}', 'Json result={{"allocation_calls",experiment::allocation_calls.load()},{"allocation_ns",experiment::allocation_ns.load()},{"new_policy_stats",experiment::policyStats()},{"new_policy_calls",experiment::policy_calls.load()},{"new_policy_ns",experiment::policy_ns.load()},{"config",config}')
 (b/'candidate_real.cpp').write_text(real)
 # E's real-thread barrier remains, but all non-thread-safe legacy trace
 # callbacks are removed. New schedulers coordinate their own reservations.
 e_quota=quota.replace('#include "policy_adapter.h"', '#include "policy_adapter.h"\n#include "concurrency.h"')
 e_quota=e_quota.replace('experiment::dCandidates(candidates);','experiment::parallelCandidates(candidates);')
 e_quota=e_quota.replace('        experiment::dBatch(tl_call_count, probe_mode, total_length);','')
 (b/'quota.candidate_e.cpp').write_text(e_quota)
 e_driver=(ROOT/'simulator/driver_e.cpp').read_text().replace('#include "driver.cpp"','#include "candidate_model.inc"')
 e_driver='#include "allocation.h"\n'+e_driver
 e_driver=e_driver.replace('input>>config;', 'input>>config;experiment::allocation_mode=config.value("allocation_mode",std::string("original"))=="remainder"?1:0;')
 e_driver=e_driver.replace('s.post=now;++posted[rail];','s.post=now;experiment::policyPosted(selectors[s.owner].get(),rail,s.bytes,now);++posted[rail];')
 e_driver=e_driver.replace('ok(selectors[s.owner]->release(s.rail,s.charge,(now-s.post)/1e9));','experiment::policyCompleted(selectors[s.owner].get(),s.rail,s.charge,s.post,now);ok(selectors[s.owner]->release(s.rail,s.charge,(now-s.post)/1e9));')
 e_driver=e_driver.replace('Json result={{"config",config}', 'Json result={{"new_policy_stats",experiment::policyStats()},{"allocation_calls",experiment::allocation_calls.load()},{"allocation_ns",experiment::allocation_ns.load()},{"config",config}')
 e_driver+='\nnamespace experiment { void policyDecision(const std::vector<mr::Candidate>& c,const mr::Assignment& out) { for(size_t i=0;i<c.size();++i)if(c[i].id==0)first_p=out.weights[i]; } }\n'
 (b/'candidate_concurrent.cpp').write_text(e_driver)
 commands=[]
 for name,driver in [('candidate_sim',b/'candidate_sim.cpp'),('candidate_real',b/'candidate_real.cpp'),('candidate_concurrent',b/'candidate_concurrent.cpp')]:
  if a.sanitize and name=='candidate_real':continue
  cmd=[str(driver) if x==str(ROOT/'simulator/driver.cpp') else str(b/'quota.candidates.cpp') if x==str(b/'quota.observed.cpp') else str(b/name) if x==str(b/'observed') else x for x in command]
  if name=='candidate_concurrent':cmd=[str(b/'quota.candidate_e.cpp') if x==str(b/'quota.candidates.cpp') else x for x in cmd]
  cmd+=['-I'+str(ROOT/'algorithms'),str(ROOT/'algorithms/rail_scheduler.cpp')]
  if name=='candidate_real':cmd+=['-lrdmacm','-libverbs']
  subprocess.run(cmd,check=True);commands.append(cmd)
 cmd=['g++','-std=c++17','-O2','-g','-pthread','-I'+str(ROOT/'algorithms'),str(ROOT/'tests/candidate_algorithms.cpp'),str(ROOT/'algorithms/rail_scheduler.cpp'),'-o',str(b/'candidate_tests')]
 if a.sanitize:cmd+=['-fsanitize=address,undefined','-fno-omit-frame-pointer']
 subprocess.run(cmd,check=True);commands.append(cmd)
 cmd=['g++','-std=c++17','-O2','-pthread','-I'+str(ROOT/'algorithms'),str(ROOT/'tests/capacity_overlap.cpp'),str(ROOT/'algorithms/rail_scheduler.cpp'),'-o',str(b/'capacity_overlap')]
 if a.sanitize:cmd+=['-fsanitize=address,undefined','-fno-omit-frame-pointer']
 subprocess.run(cmd,check=True);commands.append(cmd)
 files=[ROOT/'algorithms/rail_scheduler.h',ROOT/'algorithms/rail_scheduler.cpp',ROOT/'simulator/policy_adapter.h',ROOT/'simulator/policy_metrics.inc',ROOT/'tests/candidate_algorithms.cpp',ROOT/'tests/capacity_overlap.cpp',pathlib.Path(__file__)]
 (b/'manifest-candidates.json').write_text(json.dumps({'base':base,'commands':commands,'source_sha256':{str(f.relative_to(ROOT)):hashlib.sha256(f.read_bytes()).hexdigest() for f in files},'binary_sha256':{n:hashlib.sha256((b/n).read_bytes()).hexdigest() for n in ['candidate_sim','candidate_tests','candidate_concurrent','capacity_overlap']+([] if a.sanitize else ['candidate_real'])}},indent=2)+'\n');print('CANDIDATES_BUILD_OK',flush=True)
if __name__=='__main__':main()
