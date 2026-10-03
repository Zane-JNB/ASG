import pytest
from scheduler.db import connect, get_or_create_student

@pytest.fixture
def conn(): return connect(":memory:")                      

@pytest.fixture
def sid(conn): return get_or_create_student(conn, "Zane")   