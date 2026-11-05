import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if report["meta"]["branch_coverage"] is not True:
    raise SystemExit("Branch coverage must be enabled.")

totals = report["totals"]
failed = False
for label, covered_key, total_key, minimum in (
    ("line", "covered_lines", "num_statements", 90),
    ("branch", "covered_branches", "num_branches", 85),
):
    covered, total = totals[covered_key], totals[total_key]
    if (
        type(covered) is not int
        or type(total) is not int
        or not 0 <= covered <= total
        or (label == "line" and total == 0)
    ):
        raise SystemExit(f"Invalid {label} coverage counts.")
    percent = covered * 100 / total if total else 100
    print(f"{label.capitalize()} coverage: {percent:.2f}% (minimum {minimum}%)")
    # Compare counts without rounding so a value below the floor never passes.
    failed |= covered * 100 < total * minimum

raise SystemExit(1 if failed else 0)
