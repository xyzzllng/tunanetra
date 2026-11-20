"""Fixture bersama untuk test yang tidak membutuhkan torch."""
import numpy as np
import pytest


@pytest.fixture
def frame():
    return np.full((480, 640, 3), 128, np.uint8)
