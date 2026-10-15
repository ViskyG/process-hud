"""Keep documented CLI flags and local Markdown links consistent with the repo.

Parsing example placeholders is not a live test of a remote backend or workload.
"""
from pathlib import Path
import re
import shlex

import pytest

from process_hud.cli import parser


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = sorted([*ROOT.glob('*.md'), *ROOT.glob('docs/*.md'),
                    *ROOT.glob('skills/**/SKILL.md')])


def examples():
    for path in DOCUMENTS:
        for block in re.finditer(r'```bash\n(.*?)```', path.read_text(), re.S):
            for line in block.group(1).replace('\\\n', ' ').splitlines():
                args = shlex.split(line)
                if args and args[0].split('/')[-1] == 'process-hud':
                    yield str(path.relative_to(ROOT))+':'+str(block.start()), args[1:]


CASES = list(examples())


@pytest.mark.parametrize('location,args', CASES, ids=[case[0] for case in CASES])
def test_documented_cli_arguments_parse(location, args):
    parser().parse_args(args)


def test_local_documentation_links_exist():
    for path in DOCUMENTS:
        for target in re.findall(r'\]\(([^)]+)\)', path.read_text()):
            if '://' in target or target.startswith('#'):
                continue
            local = target.split('#', 1)[0]
            assert not local or (path.parent/local).exists(), (path, target)
