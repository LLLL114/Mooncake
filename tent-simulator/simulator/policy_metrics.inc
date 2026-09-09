namespace experiment {
void policyDecision(const std::vector<mr::Candidate>& c,const mr::Assignment& out) {
    if(!aggregate || now_ns<metric_start || now_ns>=metric_stop)return;
    ++decision_count;std::array<double,8> weights{};int best=c[0].id;double maximum=-1;
    for(size_t i=0;i<c.size();++i){if(c[i].id<8)weights[c[i].id]=out.weights[i];if(out.weights[i]>maximum){maximum=out.weights[i];best=c[i].id;}}
    if(previous_best>=0 && previous_best!=best)++best_changes;previous_best=best;
    if(have_weights)for(size_t i=0;i<8;++i)decision_tv+=std::abs(weights[i]-previous_weights[i])/2;
    previous_weights=weights;have_weights=true;
}
}
