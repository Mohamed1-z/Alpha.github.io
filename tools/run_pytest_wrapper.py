import pytest
import sys

print('PYTEST', pytest.__version__)
rc = pytest.main(['-q'])
print('PYTEST_RC', rc)
sys.exit(rc)
