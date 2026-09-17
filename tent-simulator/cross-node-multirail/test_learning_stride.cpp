#include "learning_stride.h"

#include <atomic>
#include <cerrno>
#include <cstdio>
#include <thread>
#include <vector>

int main() {
    if (tent_e_set_stride(0) != -EINVAL || tent_e_set_stride(1000001) != -EINVAL)
        return 1;
    for (unsigned k : {1U, 10U, 100U, 1U, 10U}) {
        if (tent_e_set_stride(k)) return 2;
        std::atomic<unsigned> failures{0};
        auto check = [&]() {
            unsigned a = 0, b = 0;
            for (unsigned i = 0; i < 1234; ++i) {
                a += tent_e_should_learn(0);
                if (i < 357) b += tent_e_should_learn(1);
            }
            if (a != 1234 / k || b != 357 / k) ++failures;
        };
        check(); // Reuses this thread across stride changes.
        std::vector<std::thread> workers;
        for (unsigned i = 0; i < 8; ++i) workers.emplace_back(check);
        for (auto& worker : workers) worker.join();
        if (failures.load() || tent_e_error()) return 3;
    }
    if (!tent_e_should_learn(64) || tent_e_error() != -ERANGE) return 4;
    std::puts("LEARNING_STRIDE_TEST_PASS: independent NIC/thread counts, changes, invalid input");
    return 0;
}
