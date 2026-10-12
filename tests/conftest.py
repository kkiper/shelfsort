import pytest

from shelfsort import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "test.db"))
    yield c
    c.close()
