// Server-only read-only bridge to the topology of the actual C API engine.
#include <cstring>
#include "tent/runtime/transfer_engine_impl.h"
#include "tent/runtime/topology.h"

extern "C" int tent_test_topology(void* engine, char* output, size_t capacity) {
    if (!engine || !output) return -1;
    const auto topology =
        static_cast<mooncake::tent::TransferEngineImpl*>(engine)->getLocalTopology();
    if (!topology) return -1;
    const auto text = topology->toString();
    if (capacity <= text.size()) return -2;
    std::memcpy(output, text.c_str(), text.size() + 1);
    return 0;
}
