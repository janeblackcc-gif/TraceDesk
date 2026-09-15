"""Reject orphan, duplicate, and unknown requirement/task/acceptance identifiers."""
from __future__ import annotations

import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / 'docs/industrial/specification/design-baseline-1.0'


def validate(root: Path = ROOT) -> dict[str, int]:
    spec = root / 'docs/industrial/specification/design-baseline-1.0'
    requirements = set(re.findall(r'^\| (PC-\d+) \|', (spec / '01_产品范围与验收契约.md').read_text(encoding='utf-8'), re.M))
    tasks = set(re.findall(r'^## (T-\d+)', (spec / '06_逐项开发任务清单.md').read_text(encoding='utf-8'), re.M))
    acceptance_text = (spec / '08_最终验收与真实试用手册.md').read_text(encoding='utf-8')
    acceptance_text += '\n' + (root / 'docs/industrial/acceptance-supplement.md').read_text(encoding='utf-8')
    acceptances = set(re.findall(r'^#{2,3} (FA-\d+)', acceptance_text, re.M))
    seen_tasks: set[str] = set()
    seen_requirements: set[str] = set()
    seen_acceptances: set[str] = set()
    with (root / 'docs/traceability.csv').open(encoding='utf-8', newline='') as stream:
        for row in csv.DictReader(stream):
            task = row['task_id']
            req = set(row['requirement_ids'].split(';'))
            acc = set(row['acceptance_ids'].split(';'))
            if task in seen_tasks or task not in tasks or not req <= requirements or not acc <= acceptances:
                raise ValueError(f'Invalid traceability row: {task}')
            seen_tasks.add(task)
            seen_requirements.update(req)
            seen_acceptances.update(acc)
    for name, expected, actual in [('task', tasks, seen_tasks), ('requirement', requirements, seen_requirements),
                                    ('acceptance', acceptances, seen_acceptances)]:
        if not expected or actual != expected:
            raise ValueError(f'Orphan {name}: {sorted(expected - actual)}')
    return {'tasks': len(tasks), 'requirements': len(requirements), 'acceptances': len(acceptances)}


if __name__ == '__main__':
    print(validate())
