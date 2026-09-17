#include "osc_trace.h"

#include <cerrno>
#include <cstdio>
#include <string>
#include <thread>
#include <vector>

int main(int argc, char** argv) {
    if (argc != 3) return 1;
    const std::string mode(argv[1]);
    if (tent_osc_configure(0, 0, 0) != -EINVAL ||
        tent_osc_configure(0, 5, 16) != -EINVAL) return 2;
    const unsigned capacity = mode == "overflow" ? 2 : 16;
    if (tent_osc_configure(1, 10, capacity) ||
        tent_osc_configure(0, 0, 16) != -EALREADY) return 3;
    if (!tent_osc_fixed() || tent_osc_bandwidth() != 1250000000.) return 4;
    auto append = [](unsigned tag) {
        for (unsigned i = 0; i < 4; ++i) {
            tent_osc::Record r{};
            r.ns = 1000 + tag * 10 + i;
            r.total_bytes = 1048576;
            r.selector = 99;
            r.slices = 16; r.mode = 1; r.context = 7; r.site = 1;
            r.dev[0] = 0; r.dev[1] = 1;
            r.assigned[0] = r.assigned[1] = 524288;
            r.weight[0] = .25; r.weight[1] = .75;
            tent_osc::append(r);
        }
    };
    if (mode == "overflow") append(0);
    else {
        std::vector<std::thread> threads;
        for (unsigned i = 0; i < (mode == "owners" ? 5U : 2U); ++i)
            threads.emplace_back(append, i);
        for (auto& thread : threads) thread.join();
    }
    const int expected = mode == "basic" ? 0 : EOVERFLOW;
    if (tent_osc_error() != expected || tent_osc_dump(argv[2])) return 5;
    if (tent_osc_dump(argv[2]) != -EALREADY) return 6;
    std::printf("OSC_TRACE_TEST_PASS %s error=%d\n", mode.c_str(), expected);
    return 0;
}
