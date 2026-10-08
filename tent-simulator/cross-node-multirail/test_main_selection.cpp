#include "main_selection_trace.h"
#include <cassert>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <thread>
#include <time.h>

uint64_t now() { timespec t{};clock_gettime(CLOCK_MONOTONIC,&t);return uint64_t(t.tv_sec)*1000000000+t.tv_nsec; }
void example(bool probe) {
    std::vector<unsigned char> payload(1048576);uint64_t epoch=probe ? 2 : 1;
    std::memcpy(payload.data(),&epoch,8);
    main_trace::RequestScope request(payload.data(),payload.size());
    std::vector<int> output(16,0);
    for (size_t i=probe ? 8 : 12;i<output.size();++i) output[i]=1;
    main_trace::Allocation a(reinterpret_cast<void*>(123),1048576,16,65536,1e-12,output);
    main_trace::candidate(1,3,300,100,1);main_trace::candidate(0,1,100,200,1);
    main_trace::probe(probe);
    if (!probe) { main_trace::weight(1,1./3,4./3);main_trace::weight(0,1,4./3); }
    main_trace::success();
}
int main(int argc,char** argv) {
    assert(argc==2 || argc==3);
    auto start=now();tent_obs_configure(1,start,start+10000000000);
    example(false);example(true);
    if (argc==3) {
        std::thread other([]{example(false);});other.join();
        assert(tent_obs_last_error()!=0);return 0;
    }
    {
        std::vector<int> output{0,1};
        main_trace::Allocation a(reinterpret_cast<void*>(123),1000,2,400,1e-12,output);
        main_trace::candidate(0,1,0,100,1);main_trace::candidate(1,1,0,100,1);
        main_trace::weight(0,1,2);main_trace::weight(1,1,2);main_trace::success();
    }
    tent_obs_configure(0,0,0);example(false); // Disabled hooks must not append.
    const auto path=(std::filesystem::path(argv[1])/"observer.json").string();tent_obs_dump(path.c_str());
    assert(tent_obs_last_error()==0);
    std::ifstream f(std::filesystem::path(argv[1])/"main-selection.bin",std::ios::binary);
    tent_osc::Record r[3]{};f.read(reinterpret_cast<char*>(r),sizeof r);assert(f);
    assert(r[0].dev[0]==0 && r[0].dev[1]==1 && r[0].weight[0]==.75);
    assert(r[0].assigned[0]==786432 && r[0].assigned[1]==262144);
    assert(r[0].context==1 && r[1].context==2 && r[0].site==1);
    assert(r[1].mode==2 && r[1].assigned[0]==524288 && r[1].assigned[1]==524288);
    assert(r[2].assigned[0]==400 && r[2].assigned[1]==600 && r[2].sequence==2);
    assert(f.peek()==std::ifstream::traits_type::eof());
}
