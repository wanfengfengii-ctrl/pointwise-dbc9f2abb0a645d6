import source
from features import compute

class Warehouse:
    def __init__(self, samples):
        self.db = source.connect(samples)

    def read(self):
        return [[r[0], int(r[1]), r[2]] for r in self.db.execute(
            "SELECT sample_id,total,n FROM features ORDER BY sample_id")]

    def apply(self, rows):
        trace = []
        before = self.read()
        with self.db:
            accepted = source.import_versions(self.db, rows, trace)
            versions = source.all_versions(self.db, trace)
            samples = source.all_samples(self.db, trace)
            values = compute(versions, samples)
            for sid, total, n in values:
                self.db.execute(
                    "UPDATE features SET total=?,n=? WHERE sample_id=?",
                    (str(total), n, sid))
            trace.append(["feature_writes", len(values)])
        old = {r[0]: r[1:] for r in before}
        return {
            "accepted": accepted,
            "features": values,
            "changed": [r[0] for r in values if old[r[0]] != r[1:]],
            "trace": trace,
        }

    def close(self):
        self.db.close()
