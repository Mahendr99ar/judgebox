from judgebox.judge import checkers


def test_tokens_ignores_whitespace_layout():
    assert checkers.tokens(b"1 2\n3\n", b"1\n2 3").accepted
    r = checkers.tokens(b"1 2 4", b"1 2 3")
    assert not r.accepted and "token 3" in r.message


def test_tokens_counts_missing_output():
    assert not checkers.tokens(b"1 2", b"1 2 3").accepted
    assert not checkers.tokens(b"", b"0").accepted


def test_exact_ignores_trailing_spaces_and_blank_lines_only():
    assert checkers.exact(b"a b  \r\nc\n\n\n", b"a b\nc").accepted
    assert not checkers.exact(b"a  b\nc", b"a b\nc").accepted


def test_floats_within_tolerance():
    assert checkers.floats(b"3.1415926", b"3.14159265", 1e-6).accepted
    assert checkers.floats(b"1000000.5", b"1000000.0", 1e-6).accepted  # relative
    assert not checkers.floats(b"3.14", b"3.14159265", 1e-6).accepted


def test_floats_rejects_nan_and_mismatched_words():
    assert not checkers.floats(b"nan", b"1.0", 1e-6).accepted
    assert not checkers.floats(b"YES 1.0", b"NO 1.0", 1e-6).accepted
