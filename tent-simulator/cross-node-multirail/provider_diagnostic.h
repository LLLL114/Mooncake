#pragma once
#include <cstdint>
namespace tent_provider {
extern thread_local bool entered;
void poll(int worker, int dev, uint64_t begin, uint64_t end, int count) noexcept;
void dump(const char* directory);
}
