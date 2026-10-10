#!/usr/bin/env python3
"""Generate the `learning-pairs/` dataset: does a lesson transfer between tasks?

    python scripts/make_learning_pairs.py        # (re)writes learning-pairs/

A held-out benchmark run is an honest but blunt test of learning: unrelated
tasks, few shared lessons, one run each. These tasks are built so a lesson
either transfers or it does not, and so the model cannot already know it.

Every task uses `quartzctl`, a tool invented for this purpose, with two quirks
that its `--help` does not mention and its error messages do not explain:

* `export` fails with `QZ-4401: session not armed` until the session is armed
  with a token kept in /etc/quartz/arm.token;
* `import` fails with `QZ-2207: missing or wrong trailer` unless the file ends
  with a `#QZEND <rows>` line.

The fixes are in a long troubleshooting document on the machine, so every task
is solvable cold — it just takes finding and reading it. There are three tasks
per quirk: two to learn from (NovaCode shares a lesson only once a second
project has confirmed it) and one to test on.

    train:  qz-export-1  qz-export-2  qz-import-1  qz-import-2
    test:   qz-export-test            qz-import-test
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "learning-pairs"
TOKEN = "qz-7f3a91c2e5"

QUARTZCTL = r"""#!/bin/bash
# quartzctl - Quartz dataset tool (invented for the learning-pairs eval).
STATE=/var/lib/quartz/datasets
case "$1" in
  ""|--help|-h|help)
    cat <<'EOF'
quartzctl - Quartz dataset tool

Usage:
  quartzctl list                  list datasets
  quartzctl export <name>         print a dataset as CSV
  quartzctl import <name> <file>  create a dataset from a CSV file
  quartzctl status                show session status

Documentation: /usr/share/doc/quartz/
EOF
    ;;
  list)
    ls "$STATE" 2>/dev/null | sed 's/\.qz$//'
    ;;
  status)
    if [ -f /run/quartz.armed ]; then echo "session: armed"; else echo "session: idle"; fi
    ;;
  arm)
    if [ "$2" = "--token" ] && [ -n "$3" ] && [ "$3" = "$(cat /etc/quartz/arm.token)" ]; then
      touch /run/quartz.armed
      echo "session armed"
    else
      echo "QZ-4402: bad or missing token" >&2
      exit 2
    fi
    ;;
  export)
    if [ ! -f /run/quartz.armed ]; then echo "QZ-4401: session not armed" >&2; exit 3; fi
    if [ ! -f "$STATE/$2.qz" ]; then echo "QZ-1001: no such dataset: $2" >&2; exit 4; fi
    base64 -d "$STATE/$2.qz" | gunzip
    ;;
  import)
    name="$2"; file="$3"
    if [ -z "$name" ] || [ ! -f "$file" ]; then echo "QZ-1002: no such file: $file" >&2; exit 4; fi
    rows=$(( $(grep -c '' "$file") - 2 ))
    if [ "$(tail -n 1 "$file")" != "#QZEND $rows" ]; then
      echo "QZ-2207: missing or wrong trailer" >&2
      exit 5
    fi
    head -n -1 "$file" | gzip -n -c | base64 > "$STATE/$name.qz"
    echo "imported $name ($rows rows)"
    ;;
  *)
    echo "QZ-0001: unknown command: $1" >&2
    exit 1
    ;;
esac
"""

DOCKERFILE = """FROM ubuntu:24.04

COPY quartzctl /usr/local/bin/quartzctl
COPY TROUBLESHOOTING.md /usr/share/doc/quartz/TROUBLESHOOTING.md
COPY seed/ /tmp/seed/
COPY data/ /app/data/

RUN chmod +x /usr/local/bin/quartzctl \\
 && mkdir -p /etc/quartz /var/lib/quartz/datasets \\
 && printf '%s' '{token}' > /etc/quartz/arm.token \\
 && for f in /tmp/seed/*.csv; do \\
      gzip -n -c "$f" | base64 > "/var/lib/quartz/datasets/$(basename "$f" .csv).qz"; \\
    done \\
 && rm -rf /tmp/seed

WORKDIR /app
"""

TASK_TOML = """schema_version = "1.1"

[task]
name = "nova-learning/{name}"
description = "{description}"
keywords = ["learning-transfer", "quartzctl"]

[metadata]
difficulty = "easy"
category = "learning-transfer"

[verifier]
timeout_sec = 120.0

[agent]
timeout_sec = 600.0

[environment]
build_timeout_sec = 600.0
cpus = 1
memory_mb = 1024
storage_mb = 4096
gpus = 0
allow_internet = true
"""

RULES = (
    "\n\nUse the `quartzctl` tool installed on this machine. Do not modify `quartzctl`, and do "
    "not read or write the files under `/var/lib/quartz` directly.\n"
)

# name -> rows (header first). Small, distinct, deterministic.
SEED = {
    "inventory": ["sku,name,qty", "A100,widget,14", "A200,gasket,3", "A300,bracket,27", "A400,spindle,9"],
    "orders": ["order_id,sku,units", "5001,A100,2", "5002,A300,5", "5003,A100,1", "5004,A400,3", "5005,A200,8", "5006,A300,2"],
    "customers": ["customer_id,region", "C1,north", "C2,south", "C3,north"],
    "shipments": ["shipment_id,order_id,carrier,weight_kg", "S900,5001,polar,1.2", "S901,5002,ridge,4.8", "S902,5004,polar,2.1"],
}
DATA = {
    "new_items": ["sku,name,qty", "B100,flange,6", "B200,coupler,11", "B300,gear,4"],
    "returns": ["return_id,order_id,reason", "R1,5002,damaged", "R2,5005,wrong item"],
    "suppliers": ["supplier_id,name,country", "P1,Altamont,NZ", "P2,Brightwell,CL", "P3,Corvane,FI", "P4,Dunmore,IE"],
    "depots": ["depot_id,city", "D1,Harrow", "D2,Lindale"],
}


def csv(rows: list[str]) -> str:
    return "\n".join(rows) + "\n"


def troubleshooting() -> str:
    """A long reference document; the two entries that matter are buried in it."""
    subsystems = ["catalog", "session", "codec", "ledger", "index", "scheduler", "transport", "quota"]
    lines = [
        "# Quartz troubleshooting reference",
        "",
        "Error codes are listed in numerical order. Most are informational.",
        "",
    ]
    real = {
        2207: (
            "QZ-2207: missing or wrong trailer",
            "A file given to `quartzctl import` must end with a trailer line of the form "
            "`#QZEND <n>`, where `<n>` is the number of data rows (the header line is not "
            "counted, and neither is the trailer). Append the trailer to the file, then import "
            "again. Example for a file with a header and 3 data rows: `echo '#QZEND 3' >> file.csv`.",
        ),
        4401: (
            "QZ-4401: session not armed",
            "`quartzctl export` only works in an armed session. Arm it once with "
            '`quartzctl arm --token "$(cat /etc/quartz/arm.token)"`, then run the export again. '
            "`quartzctl status` shows whether the session is armed. The `arm` command is not "
            "listed in `--help`.",
        ),
    }
    for i, code in enumerate(sorted({*range(1001, 9000, 53), *real})):
        if code in real:
            title, text = real[code]
        else:
            sub = subsystems[i % len(subsystems)]
            title = f"QZ-{code}: {sub} notice {i % 17}"
            text = (
                f"Raised by the {sub} subsystem during routine operation. No action is needed "
                f"unless it repeats more than {3 + i % 5} times in a minute, in which case restart "
                f"the {sub} worker and check its log for the preceding entry."
            )
        lines += [f"## {title}", "", text, ""]
    return "\n".join(lines)


def verifier(checks: list[str]) -> str:
    return "\n".join(
        [
            "#!/bin/bash",
            "# Verifier. Always writes a reward, pass or fail (no `set -e`).",
            "mkdir -p /logs/verifier",
            "pass=1",
            'same() { # same <label> <actual-file> <expected-file>',
            '  if [ -f "$2" ] && diff -q <(tr -d "\\r" < "$2") "$3" >/dev/null 2>&1; then echo "ok: $1"',
            '  else echo "FAIL: $1"; pass=0; fi',
            "}",
            'dataset() { # dataset <name>: the imported dataset must decode to the expected rows',
            '  if base64 -d "/var/lib/quartz/datasets/$1.qz" 2>/dev/null | gunzip 2>/dev/null > "/tmp/$1.out"; then',
            '    same "dataset $1" "/tmp/$1.out" "/tests/expected/$1.csv"',
            '  else echo "FAIL: dataset $1 missing or unreadable"; pass=0; fi',
            "}",
            *checks,
            'echo "$pass" > /logs/verifier/reward.txt',
            'echo "reward: $pass"',
            "",
        ]
    )


ARM = 'quartzctl arm --token "$(cat /etc/quartz/arm.token)"'

TASKS = {
    "qz-export-1": {
        "description": "Export one dataset with quartzctl.",
        "instruction": "Export the Quartz dataset `inventory` to `/app/inventory.csv`. The file must "
        "contain exactly what `quartzctl` prints for that dataset.",
        "solve": [ARM, "quartzctl export inventory > /app/inventory.csv"],
        "expected": {"inventory.csv": csv(SEED["inventory"])},
        "checks": ['same "inventory.csv" /app/inventory.csv /tests/expected/inventory.csv'],
    },
    "qz-export-2": {
        "description": "Count the rows of two datasets exported with quartzctl.",
        "instruction": "Write `/app/counts.txt` with two lines, `orders <n>` and `customers <n>`, where "
        "`<n>` is the number of data rows (not counting the header line) in the Quartz datasets "
        "`orders` and `customers`.",
        "solve": [
            ARM,
            'o=$(( $(quartzctl export orders | grep -c "") - 1 ))',
            'c=$(( $(quartzctl export customers | grep -c "") - 1 ))',
            'printf "orders %s\\ncustomers %s\\n" "$o" "$c" > /app/counts.txt',
        ],
        "expected": {"counts.txt": f"orders {len(SEED['orders']) - 1}\ncustomers {len(SEED['customers']) - 1}\n"},
        "checks": ['same "counts.txt" /app/counts.txt /tests/expected/counts.txt'],
    },
    "qz-export-test": {
        "description": "Extract the header of a dataset exported with quartzctl.",
        "instruction": "Write the header line (the first line) of the Quartz dataset `shipments` to "
        "`/app/header.txt`.",
        "solve": [ARM, "quartzctl export shipments | head -n 1 > /app/header.txt"],
        "expected": {"header.txt": SEED["shipments"][0] + "\n"},
        "checks": ['same "header.txt" /app/header.txt /tests/expected/header.txt'],
    },
    "qz-import-1": {
        "description": "Import one CSV file with quartzctl.",
        "instruction": "Import `/app/data/new_items.csv` into Quartz as a dataset named `items`. The "
        "dataset must hold exactly the header and data rows of that file.",
        "solve": ["cp /app/data/new_items.csv /tmp/i.csv", "echo '#QZEND 3' >> /tmp/i.csv", "quartzctl import items /tmp/i.csv"],
        "expected": {"items.csv": csv(DATA["new_items"])},
        "checks": ["dataset items"],
    },
    "qz-import-2": {
        "description": "Import a returns file with quartzctl.",
        "instruction": "Import `/app/data/returns.csv` into Quartz as a dataset named `returns`. The "
        "dataset must hold exactly the header and data rows of that file.",
        "solve": ["cp /app/data/returns.csv /tmp/r.csv", "echo '#QZEND 2' >> /tmp/r.csv", "quartzctl import returns /tmp/r.csv"],
        "expected": {"returns.csv": csv(DATA["returns"])},
        "checks": ["dataset returns"],
    },
    "qz-import-test": {
        "description": "Import two CSV files with quartzctl.",
        "instruction": "Import `/app/data/suppliers.csv` into Quartz as a dataset named `suppliers`, "
        "and `/app/data/depots.csv` as a dataset named `depots`. Each dataset must hold exactly "
        "the header and data rows of its file.",
        "solve": [
            "cp /app/data/suppliers.csv /tmp/s.csv && echo '#QZEND 4' >> /tmp/s.csv && quartzctl import suppliers /tmp/s.csv",
            "cp /app/data/depots.csv /tmp/d.csv && echo '#QZEND 2' >> /tmp/d.csv && quartzctl import depots /tmp/d.csv",
        ],
        "expected": {"suppliers.csv": csv(DATA["suppliers"]), "depots.csv": csv(DATA["depots"])},
        "checks": ["dataset suppliers", "dataset depots"],
    },
}


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))  # bytes: LF endings, whatever the host OS


def main() -> None:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    for name, task in TASKS.items():
        base = ROOT / name
        write(base / "instruction.md", task["instruction"] + RULES)
        write(base / "task.toml", TASK_TOML.format(name=name, description=task["description"]))
        env = base / "environment"
        write(env / "Dockerfile", DOCKERFILE.format(token=TOKEN))
        write(env / "quartzctl", QUARTZCTL.lstrip("\n"))
        write(env / "TROUBLESHOOTING.md", troubleshooting())
        for dataset, rows in SEED.items():
            write(env / "seed" / f"{dataset}.csv", csv(rows))
        for dataset, rows in DATA.items():
            write(env / "data" / f"{dataset}.csv", csv(rows))
        write(base / "solution" / "solve.sh", "#!/bin/bash\nset -e\n" + "\n".join(task["solve"]) + "\n")
        write(base / "tests" / "test.sh", verifier(task["checks"]))
        for filename, text in task["expected"].items():
            write(base / "tests" / "expected" / filename, text)
    doc = troubleshooting()
    assert "QZ-4401: session not armed" in doc and "QZ-2207: missing or wrong trailer" in doc
    print(f"wrote {len(TASKS)} tasks to {ROOT} (troubleshooting doc: {doc.count('## QZ-')} entries)")


if __name__ == "__main__":
    main()
