#pragma once
#include "tent/transport/rdma/quota.h"

namespace experiment {
inline uint64_t now_ns = 0;
inline bool observe = false;
inline bool wanted = false;
inline bool aggregate = false;
bool sampleGate(int device, double observed_bps, double& alpha);
void candidates(const std::vector<mooncake::tent::DeviceSelector::Candidate>&,
                uint64_t, int);
}
