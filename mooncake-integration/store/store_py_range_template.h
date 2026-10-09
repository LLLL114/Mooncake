// Immutable byte geometry shared by repeated ranged reads. No addresses,
// object keys, replicas or leases survive in a template.
class RangedReadTemplatePy {
   public:
    const std::vector<size_t> destinations, sources, sizes;
    size_t max_destination_end = 0;
    size_t total_bytes = 0;

    RangedReadTemplatePy(std::vector<size_t> dst, std::vector<size_t> src,
                         std::vector<size_t> lengths)
        : destinations(std::move(dst)),
          sources(std::move(src)),
          sizes(std::move(lengths)) {
        if (sizes.empty() || sizes.size() > 1000000 ||
            destinations.size() != sizes.size() ||
            sources.size() != sizes.size())
            throw std::invalid_argument("Invalid ranged-read template shape");
        for (size_t i = 0; i < sizes.size(); ++i) {
            const auto size = sizes[i];
            if (!size || size > (1ULL << 40) - total_bytes ||
                destinations[i] > SIZE_MAX - size ||
                sources[i] > SIZE_MAX - size)
                throw std::invalid_argument(
                    "Ranged-read template exceeds bounds");
            total_bytes += size;
            max_destination_end =
                std::max(max_destination_end, destinations[i] + size);
        }
    }
};

std::vector<bool> read_range_templates(
    RangedReadSnapshotPy &snapshot,
    const std::vector<std::shared_ptr<RangedReadTemplatePy>> &templates,
    const std::vector<uintptr_t> &buffer_ptrs,
    const std::vector<size_t> &buffer_indices,
    const std::vector<std::string> &keys,
    const std::vector<size_t> &translations, bool allow_staging) {
    const auto n = keys.size();
    if (n > 100000 || buffer_ptrs.size() > 100000 || templates.size() != n ||
        buffer_indices.size() != n || translations.size() != n)
        throw std::invalid_argument("Ranged-read template batch shape differs");
    size_t operations = 0, bytes = 0;
    for (size_t i = 0; i < n; ++i) {
        const auto &plan = templates[i];
        if (!plan || buffer_indices[i] >= buffer_ptrs.size() ||
            translations[i] > SIZE_MAX - plan->max_destination_end ||
            plan->sizes.size() > 1000000 - operations ||
            plan->total_bytes > (1ULL << 40) - bytes)
            throw std::invalid_argument(
                "Ranged-read template batch exceeds bounds");
        operations += plan->sizes.size();
        bytes += plan->total_bytes;
    }
    // Expand and validate results without boxing every range through Python.
    // All per-call state stays local so one immutable template can be
    // concurrent.
    std::vector<void *> buffers;
    for (auto pointer : buffer_ptrs)
        buffers.push_back(reinterpret_cast<void *>(pointer));
    std::vector<std::vector<std::string>> grouped_keys(buffers.size());
    std::vector<std::vector<std::vector<size_t>>> dst(buffers.size()),
        src(buffers.size()), sizes(buffers.size());
    std::vector<size_t> positions(n);
    for (size_t i = 0; i < n; ++i) {
        auto region = buffer_indices[i];
        const auto &plan = *templates[i];
        positions[i] = grouped_keys[region].size();
        grouped_keys[region].push_back(keys[i]);
        auto offsets = plan.destinations;
        for (auto &offset : offsets) offset += translations[i];
        dst[region].push_back(std::move(offsets));
        src[region].push_back(plan.sources);
        sizes[region].push_back(plan.sizes);
    }
    if (!n) return {};
    auto values = snapshot.get_into_ranges(buffers, grouped_keys, dst, src,
                                           sizes, allow_staging);
    std::vector<bool> complete(n, false);
    if (values.size() != buffers.size()) return complete;
    for (size_t i = 0; i < n; ++i) {
        auto region = buffer_indices[i], position = positions[i];
        if (values[region].size() != grouped_keys[region].size()) continue;
        const auto &result = values[region][position];
        const auto &expected = templates[i]->sizes;
        if (result.size() != expected.size()) continue;
        bool ok = true;
        for (size_t k = 0; k < result.size(); ++k)
            ok &=
                result[k] >= 0 && static_cast<size_t>(result[k]) == expected[k];
        complete[i] = ok;
    }
    return complete;
}
