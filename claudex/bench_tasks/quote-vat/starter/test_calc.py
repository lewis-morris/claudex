from calc import subtotal


def test_subtotal():
    assert subtotal([(2, 150), (1, 99)]) == 399
