"""Publish each complete original Spider/BIRD panel without waiting for the others."""

import os
SEER_ROOT = os.environ.get("SEER_ROOT", ".")
import json
from pathlib import Path
import subprocess
import time

ROOT = Path((SEER_ROOT))
RUNS = ROOT / 'repro/original-home-seer-v1'
SCORE = ROOT / 'seer-original-home-panels-wt/scripts/score_original_home_panels_seer_v1.py'
COMPARE = ROOT / 'seer-lc-proof-dev-wt/scripts/compare_original_home_panels_v1.py'
PANELS = {
    'spider_sqlwitness_original_batches': 564,
    'bird_sqlwitness_original_batches': 1528,
    'bird_parseval_original_batches': 1526,
}


def main():
    pending = set(PANELS)
    while pending:
        for panel in sorted(pending):
            expected = PANELS[panel]
            results = list((RUNS / panel).glob('*/result.json'))
            if len(results) > expected:
                raise RuntimeError(f'{panel}: extra result directories')
            if len(results) != expected:
                continue
            output = ROOT / 'repro/original-panels-score-v1' / (panel + '.json')
            subprocess.run(['/usr/bin/python3', str(SCORE), '--panel', panel], check=True)
            summary = json.loads(output.read_text())
            assert summary['batches'] == expected
            assert summary['all_original_pairs'] in (1034, 1534)
            subprocess.run(['/usr/bin/python3', str(COMPARE), '--panel', panel], check=True)
            comparison = json.loads((output.parent / ('comparison-' + panel + '.json')).read_text())
            assert comparison['test_pairs'] == summary['test_pairs']
            print(json.dumps({'scored_panel': panel, 'batches': expected,
                              'pairs': summary['all_original_pairs'],
                              'test_pairs': comparison['test_pairs']}), flush=True)
            pending.remove(panel)
        if pending:
            time.sleep(30)


if __name__ == '__main__':
    main()
