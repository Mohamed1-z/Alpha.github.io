import re
from pathlib import Path

signals_dir = Path('signals')
patterns = [r'US30m']
repl = 'US30_x10m'

changed = []
for path in signals_dir.rglob('*'):
    if path.is_file() and path.suffix in ('.json', '.jsonl'):
        text = path.read_text(encoding='utf-8')
        new_text, n = re.subn('|'.join(patterns), repl, text)
        if n:
            bak = path.with_suffix(path.suffix + '.bak')
            path.replace(bak)
            bak.write_text(text, encoding='utf-8')
            path.write_text(new_text, encoding='utf-8')
            changed.append((str(path), n))

if not changed:
    print('No files changed')
else:
    for p, n in changed:
        print(f'Updated {p}: {n} replacements')
    print(f'Total files updated: {len(changed)}')
