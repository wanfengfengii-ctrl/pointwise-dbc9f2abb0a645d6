import json
import re
import sys
from pathlib import Path
from observe import load, observe
from warehouse import Warehouse

def oracle(versions, sample):
    # Deliberately slow test-only reference: no production feature functions.
    chosen = {}
    for row in sorted(versions, key=lambda r: r[1], reverse=True):
        if row[4] <= sample[3] and row[0] not in chosen:
            chosen[row[0]] = row
    result = {}
    for rid, row in chosen.items():
        active = (not row[6] and row[2] == sample[1]
                  and sample[2] - 86400 <= row[3] < sample[2])
        if active:
            result[rid] = (row[5], 1)
    return result

def expected_features(versions, samples):
    result = []
    for sample in samples:
        per_event = oracle(versions, sample)
        result.append([sample[0], sum(x[0] for x in per_event.values()),
                       sum(x[1] for x in per_event.values())])
    return sorted(result)

def states(case):
    versions = {}
    for batch in case["batches"]:
        before = list(versions.values())
        for row in batch["rows"]:
            key = tuple(row[:2])
            assert key not in versions or versions[key] == row
            versions[key] = row
        yield batch["name"], before, list(versions.values())

def normal():
    cases = load()
    actual = observe("all")
    for name, case in cases.items():
        for (_, _, after), report in zip(states(case), actual[name]):
            assert report["features"] == expected_features(after, case["samples"])
    assert actual["normal"][0]["features"] == [["n", 7, 1]]
    assert actual["normal"][1]["accepted"] == 0
    history = {r["batch"]: {x[0]: x[1:] for x in r["features"]}
               for r in actual["history"]}
    assert history["late_low"]["a10"] == [7, 3]
    assert history["late_low"]["a30"] == [30, 3]
    assert history["tombstone"]["a20"] == [0, 2]
    assert history["tombstone"]["a19"] == [7, 3]
    assert history["move"]["b30"] == [0, 0]
    assert history["move"]["b_next"] == [11, 1]
    assert history["revive_zero"]["a40"] == [0, 3]
    assert history["revive_zero"]["b40"] == [0, 0]
    assert history["shadowed"] == history["revive_zero"]
    assert history["cancel"]["a20"] == [0, 2]
    assert history["cancel"]["a_slice"] == [-8, 1]
    warehouse = Warehouse(cases["normal"]["samples"])
    try:
        warehouse.apply(cases["normal"]["batches"][0]["rows"])
        before = warehouse.read()
        source_before = list(warehouse.db.execute("SELECT * FROM versions"))
        try:
            warehouse.apply([["new", 1, "A", 0, 1, 2, False],
                             ["e", 1, "A", 0, 10, 99, False]])
        except ValueError as exc:
            assert str(exc) == "REVISION_CONFLICT"
        else:
            raise AssertionError("conflict was accepted")
        assert warehouse.read() == before
        assert list(warehouse.db.execute("SELECT * FROM versions")) == source_before
    finally:
        warehouse.close()
    print("NORMAL_REGRESSION_OK")

def review():
    normal()
    doc = Path("DESIGN.md").read_text()
    blocks = re.findall(r"~~~json\s*\n(.*?)\n~~~", doc, re.S)
    assert len(blocks) == 1, "DESIGN.md needs one ~~~json evidence block"
    evidence = json.loads(blocks[0])
    assert set(evidence) == {"observed", "candidates"}
    assert evidence["observed"] == observe("all"), "observed output differs"
    cases = load()
    assert set(evidence["candidates"]) == set(cases)
    for name, case in cases.items():
        predictions = evidence["candidates"][name]
        assert set(predictions) == {b["name"] for b in case["batches"]}
        for batch, before, after in states(case):
            required, allowed = set(), set()
            touched = {row[0] for b in case["batches"] if b["name"] == batch
                       for row in b["rows"]}
            for sample in case["samples"]:
                old, new = oracle(before, sample), oracle(after, sample)
                aggregate = lambda d: (sum(v[0] for v in d.values()),
                                       sum(v[1] for v in d.values()))
                if aggregate(old) != aggregate(new):
                    required.add(sample[0])
                def winner(rows, rid):
                    eligible = [r for r in rows
                                if r[0] == rid and r[4] <= sample[3]]
                    return max(eligible, key=lambda r: r[1], default=None)
                for rid in touched:
                    if (winner(before, rid) != winner(after, rid)
                            and (rid in old or rid in new)):
                        allowed.add(sample[0])
            predicted = predictions[batch]
            assert isinstance(predicted, list) and all(type(x) is str for x in predicted)
            assert predicted == sorted(set(predicted)), (name, batch, "order/duplicate")
            assert required <= set(predicted) <= allowed, (
                name, batch, "candidate coverage/boundary differs")
    citations = re.findall(r"([a-z]+\.py):([1-9][0-9]*)", doc)
    assert citations, "cite source.py:line style code locations"
    for file, line in citations:
        assert Path(file).is_file(), file
        assert int(line) <= len(Path(file).read_text().splitlines()), (file, line)
    print("DESIGN_EVIDENCE_OK: replayed outputs, finite candidate sets, source locations")
    print("Architecture, access bounds and pseudocode still require human review.")

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
    if mode not in {"normal", "review"}:
        raise SystemExit("usage: checks.py normal|review")
    (normal if mode == "normal" else review)()
