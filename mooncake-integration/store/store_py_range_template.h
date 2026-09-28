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

// A destination-run program. Objects only share geometry and metadata; runs
// retain the caller's logical traversal across those objects.
class RangedReadPlanPy {
   public:
    struct Object {
        std::string suffix;
        size_t region;
        std::shared_ptr<RangedReadTemplatePy> geometry;
    };
    std::vector<Object> objects;
    std::vector<std::array<size_t, 3>> runs;
    size_t operations = 0, bytes = 0;

    RangedReadPlanPy(
        const std::vector<std::shared_ptr<RangedReadTemplatePy>> &templates,
        const std::vector<std::string> &suffixes,
        const std::vector<size_t> &regions) {
        const size_t n = templates.size();
        if (!n || n > 100000 || suffixes.size() != n || regions.size() != n)
            throw std::invalid_argument("Invalid ranged-read plan shape");
        std::map<std::pair<size_t, std::string>, size_t> slots;
        std::vector<std::vector<size_t>> dst, src, sizes;
        for (size_t i = 0; i < n; ++i) {
            const auto &t = templates[i];
            if (!t || t->sizes.size() > 1000000 - operations ||
                t->total_bytes > (1ULL << 40) - bytes || regions[i] >= 100000)
                throw std::invalid_argument("Ranged-read plan exceeds bounds");
            operations += t->sizes.size();
            bytes += t->total_bytes;
            for (size_t k = 1; k < t->sizes.size(); ++k)
                if (t->destinations[k - 1] + t->sizes[k - 1] !=
                    t->destinations[k])
                    throw std::invalid_argument(
                        "Plan run destination is not contiguous");
            auto [it, inserted] =
                slots.try_emplace({regions[i], suffixes[i]}, objects.size());
            if (inserted) {
                objects.push_back({suffixes[i], regions[i], nullptr});
                dst.emplace_back();
                src.emplace_back();
                sizes.emplace_back();
            }
            const auto index = it->second;
            if (!dst[index].empty() && dst[index].back() + sizes[index].back() >
                                           t->destinations.front())
                throw std::invalid_argument(
                    "Plan object destinations are not ordered");
            const size_t first = sizes[index].size();
            dst[index].insert(dst[index].end(), t->destinations.begin(),
                              t->destinations.end());
            src[index].insert(src[index].end(), t->sources.begin(),
                              t->sources.end());
            sizes[index].insert(sizes[index].end(), t->sizes.begin(),
                                t->sizes.end());
            runs.push_back({index, first, sizes[index].size()});
        }
        for (size_t i = 0; i < objects.size(); ++i)
            objects[i].geometry = std::make_shared<RangedReadTemplatePy>(
                std::move(dst[i]), std::move(src[i]), std::move(sizes[i]));
    }
};

std::vector<bool> read_range_plans(
    RangedReadSnapshotPy &snapshot,
    const std::vector<std::shared_ptr<RangedReadPlanPy>> &plans,
    const std::vector<uintptr_t> &buffer_ptrs,
    const std::vector<std::string> &prefixes,
    const std::vector<std::vector<size_t>> &translations, bool allow_staging) {
    const size_t n = plans.size(), regions = buffer_ptrs.size();
    if (n > 100000 || regions > 100000 || prefixes.size() != n ||
        translations.size() != n)
        throw std::invalid_argument("Ranged-read plan batch shape differs");
    size_t operations = 0, bytes = 0, objects = 0;
    for (size_t page = 0; page < n; ++page) {
        const auto &plan = plans[page];
        if (!plan || translations[page].size() != regions ||
            plan->operations > 1000000 - operations ||
            plan->bytes > (1ULL << 40) - bytes ||
            plan->objects.size() > 100000 - objects)
            throw std::invalid_argument(
                "Ranged-read plan batch exceeds bounds");
        operations += plan->operations;
        bytes += plan->bytes;
        objects += plan->objects.size();
        for (const auto &object : plan->objects)
            if (object.region >= regions ||
                translations[page][object.region] >
                    SIZE_MAX - object.geometry->max_destination_end)
                throw std::invalid_argument(
                    "Ranged-read plan translation exceeds bounds");
    }
    std::vector<void *> buffers;
    for (auto pointer : buffer_ptrs)
        buffers.push_back(reinterpret_cast<void *>(pointer));
    std::vector<std::vector<std::string>> keys(regions);
    std::vector<std::vector<std::vector<size_t>>> dst(regions), src(regions),
        sizes(regions);
    std::vector<std::array<size_t, 4>> read_plan;
    struct Result {
        size_t page, region, position;
    };
    std::vector<Result> results;
    for (size_t page = 0; page < n; ++page) {
        const auto &plan = *plans[page];
        std::vector<size_t> positions;
        for (const auto &object : plan.objects) {
            const auto region = object.region;
            const auto &t = *object.geometry;
            positions.push_back(keys[region].size());
            results.push_back({page, region, keys[region].size()});
            keys[region].push_back(prefixes[page] + object.suffix);
            auto offsets = t.destinations;
            for (auto &offset : offsets) offset += translations[page][region];
            dst[region].push_back(std::move(offsets));
            src[region].push_back(t.sources);
            sizes[region].push_back(t.sizes);
        }
        for (const auto &run : plan.runs) {
            auto [object, first, end] = run;
            read_plan.push_back(
                {plan.objects[object].region, positions[object], first, end});
        }
    }
    // Logical runs are precompiled. Bind their physical page locations here;
    // sort only run references, never individual fragments in Transfer Engine.
    std::stable_sort(
        read_plan.begin(), read_plan.end(), [&](const auto &a, const auto &b) {
            return a[0] != b[0] ? a[0] < b[0]
                                : dst[a[0]][a[1]][a[2]] < dst[b[0]][b[1]][b[2]];
        });
    if (!n) return {};
    const auto values = snapshot.get_into_ranges(buffers, keys, dst, src, sizes,
                                                 allow_staging, &read_plan);
    std::vector<bool> complete(n, true);
    for (const auto &result : results) {
        auto [page, region, position] = result;
        const auto &expected = sizes[region][position];
        bool ok = region < values.size() && position < values[region].size() &&
                  values[region][position].size() == expected.size();
        if (ok)
            for (size_t k = 0; k < expected.size(); ++k)
                ok &= values[region][position][k] >= 0 &&
                      static_cast<size_t>(values[region][position][k]) ==
                          expected[k];
        complete[page] = complete[page] && ok;
    }
    return complete;
}
