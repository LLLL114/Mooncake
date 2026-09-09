#pragma once
#include "hooks.h"

namespace experiment {
// Experiment D only. Each run selects exactly one intervention.
inline int allocation_mode = 0; // 0 original, 1 remainder, 2 no probe,
                                // 3 frozen greedy, 4 refreshed greedy
void dCandidates(const std::vector<mooncake::tent::DeviceSelector::Candidate>&);
void dBatch(uint64_t call, bool probe, uint64_t bytes);
}
