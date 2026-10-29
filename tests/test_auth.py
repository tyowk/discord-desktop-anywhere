from dda.auth import Auth, RateLimiter, parse_cookies


def test_token_roundtrip_and_tamper():
    a = Auth("pw")
    t = a.make_token(now=1000)
    assert a.verify_token(t, now=1001)
    assert not a.verify_token(t, now=1000 + a.ttl + 1)
    assert not a.verify_token(t[:-1] + ("0" if t[-1] != "0" else "1"), now=1001)
    assert not a.verify_token("v1.99999999999.deadbeef", now=1)
    assert not a.verify_token("garbage") and not a.verify_token(None) and not a.verify_token("a.b.c")


def test_token_bound_to_password():
    assert not Auth("other").verify_token(Auth("pw").make_token())


def test_check_password():
    a = Auth("pw")
    assert a.check_password("pw") and not a.check_password("nope") and not a.check_password("")


def test_disabled_auth_allows_everything():
    a = Auth("")
    assert not a.enabled and a.verify_token(None) and a.check_password("x")


def test_parse_cookies():
    assert parse_cookies("a=1; dda_session=abc.def; b=") == {"a": "1", "dda_session": "abc.def", "b": ""}
    assert parse_cookies(None) == {}


def test_rate_limiter():
    r = RateLimiter(limit=3, window=10)
    for i in range(3):
        assert not r.blocked("ip", now=i)
        r.fail("ip", now=i)
    assert r.blocked("ip", now=3)
    assert not r.blocked("other", now=3)
    assert not r.blocked("ip", now=100)  # window expired
    r.fail("ip", now=100)
    r.success("ip")
    assert not r.blocked("ip", now=101)
