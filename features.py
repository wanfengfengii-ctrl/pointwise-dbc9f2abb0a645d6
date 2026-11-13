WINDOW = 86400

def contribution(version, sample):
    if version is None:
        return (0, 0)
    _, _, entity, event_time, _, amount, deleted = version
    _, wanted, t, _ = sample
    if deleted or entity != wanted or not t - WINDOW <= event_time < t:
        return (0, 0)
    return (amount, 1)

def compute(versions, samples):
    result = []
    for sample in samples:
        cutoff = sample[3]
        winners = {}
        for version in versions:
            rid, rev, _, _, available_at, _, _ = version
            if available_at <= cutoff:
                if rid not in winners or rev > winners[rid][1]:
                    winners[rid] = version
        total = n = 0
        for version in winners.values():
            amount, count = contribution(version, sample)
            total += amount
            n += count
        result.append([sample[0], total, n])
    return sorted(result)
