import json
import sys
from pathlib import Path
from warehouse import Warehouse

def load():
    return json.loads(Path(__file__).with_name("samples.json").read_text())

def run_case(case):
    warehouse = Warehouse(case["samples"])
    try:
        result = []
        for batch in case["batches"]:
            result.append({"batch": batch["name"], **warehouse.apply(batch["rows"])})
        return result
    finally:
        warehouse.close()

def observe(name):
    cases = load()
    names = sorted(cases) if name == "all" else [name]
    return {key: run_case(cases[key]) for key in names}

if __name__ == "__main__":
    print(json.dumps(observe(sys.argv[1] if len(sys.argv) > 1 else "all"),
                     ensure_ascii=False, sort_keys=True))
