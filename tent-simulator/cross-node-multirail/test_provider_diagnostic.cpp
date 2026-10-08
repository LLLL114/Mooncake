#include "poll_diagnostic.h"
#include "provider_diagnostic.h"
#include <cassert>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

int main(int argc,char** argv) {
    assert(argc==2 && tent_pd_configure()==0);
    std::vector<unsigned char> source(1048576); uint64_t epoch=7;
    std::memcpy(source.data(),&epoch,8);
    tent_pd::cycle(0,100,16); tent_pd::phase(0,110);
    tent_provider::entered=true; tent_provider::poll(0,0,120,130,0);
    tent_provider::entered=false; tent_provider::poll(0,0,220,230,0);
    tent_provider::entered=true; tent_provider::poll(0,0,320,330,64);
    tent_pd::cycle(0,2000200,16); tent_pd::phase(0,2000210);
    tent_provider::entered=true; tent_provider::poll(0,0,2000220,2000230,1);
    int token=tent_pd::complete(0,0,2,source.data(),source.size(),source.data()+65536,65536,90,115,12345);
    assert(token==0); tent_pd::handled(0,token,2000300);
    assert(tent_pd_dump(argv[1])==0);
    tent_pd::Completion r{};
    std::ifstream f(std::string(argv[1])+"/completions-0.bin",std::ios::binary);
    f.read(reinterpret_cast<char*>(&r),sizeof r);
    assert(f && r.reserved==12345 && r.qp==2);
    assert(r.previous_begin==320 && r.previous_end==330);
    assert(r.drained_begin==120 && r.drained_end==130);
    tent_pd::Gap gap{};
    std::ifstream g(std::string(argv[1])+"/poll-gaps-0.bin",std::ios::binary);
    g.read(reinterpret_cast<char*>(&gap),sizeof gap);
    assert(g && gap.previous_end==330 && gap.end==2000230);
}
