#include <cassert>
#include <cstdint>
#include <iomanip>
#include <iostream>

// Same single-caller expression as the frozen native stream's offset_ns.
long double offset(uint64_t sequence, double rate) {
    return static_cast<long double>(sequence) * 1048576 * 1000000000ULL / rate;
}
int main() {
    const long double old = offset(13494, 235824742.4);
    std::cout << std::setprecision(22) << "OLD_BOUNDARY_NS " << old
              << " delta=" << 60000000000.L - old << '\n';
    assert(old < 60000000000.L && old > 59999999999.L);
    for (uint64_t rps : {225ULL, 675ULL})
      for (uint64_t seconds : {1ULL, 3ULL, 5ULL, 8ULL, 15ULL, 60ULL}) {
        const double rate = double(rps * 1048576);
        const auto count = rps * seconds;
        assert(offset(count - 1, rate) < seconds * 1000000000.L);
        assert(offset(count, rate) == seconds * 1000000000.L);
      }
    std::cout << "INTEGER_RATE_HALF_OPEN_BOUNDARY_OK\n";
}
