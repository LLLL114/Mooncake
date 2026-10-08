#pragma once
#include <cstdint>

namespace tent_pd {
extern bool active; // Configured once before worker creation; dumped after join.
struct Completion {
    uint64_t request, offset, bytes, enqueue, submit, poll_begin, poll_end;
    uint64_t previous_begin, previous_end, drained_begin, drained_end;
    uint64_t loop_begin, phase_begin, handled;
    uint32_t worker, dev, qp, reserved;
};
struct Gap { uint64_t previous_end, begin, end, loop, phase, inflight, dev, count; };
static_assert(sizeof(Completion)==128 && sizeof(Gap)==64);
void cycle(int worker, uint64_t now, uint64_t inflight) noexcept;
void phase(int worker, uint64_t now) noexcept;
void poll(int worker, int dev, uint64_t begin, uint64_t end, int count) noexcept;
int complete(int worker, int dev, int qp, const void* source, uint64_t size,
             const void* slice_source, uint64_t bytes, uint64_t enqueue,
             uint64_t submit) noexcept;
void handled(int worker, int token, uint64_t now) noexcept;
void reject(int code) noexcept;
}
extern "C" int tent_pd_configure() noexcept;
extern "C" int tent_pd_dump(const char* directory) noexcept;
