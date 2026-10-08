#include "poll_diagnostic.h"
#include <cassert>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <vector>

int main(int argc,char** argv) {
    assert(argc==2);
    assert(tent_pd_configure()==0);
    assert(tent_pd_configure()!=0);
    std::vector<unsigned char> source(1048576); uint64_t epoch=7;
    std::memcpy(source.data(),&epoch,8);
    tent_pd::cycle(0,100,16); tent_pd::phase(0,110);
    tent_pd::poll(0,0,120,130,0);
    tent_pd::cycle(0,200,16); tent_pd::phase(0,210);
    tent_pd::poll(0,0,220,230,64); // Full budget must not replace last-drained evidence.
    tent_pd::cycle(0,2000200,16); tent_pd::phase(0,2000210);
    tent_pd::poll(0,0,2000220,2000230,1);
    const int token=tent_pd::complete(0,0,0,source.data(),source.size(),source.data()+65536,65536,90,115);
    assert(token==0); tent_pd::handled(0,token,2000300);
    assert(tent_pd_dump(argv[1])==0);
    assert(tent_pd_dump(argv[1])!=0);
    std::cout<<"POLL_DIAGNOSTIC_TEST_OK\n";
}
