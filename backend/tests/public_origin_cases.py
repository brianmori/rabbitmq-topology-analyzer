"""Configured origins also checked against Chromium's URL.origin serializer."""

PUBLIC_ORIGIN_CASES = (
    ("https://topology.example:443", "https://topology.example"),
    ("https://TOPOLOGY.EXAMPLE", "https://topology.example"),
    ("HTTPS://TOPOLOGY.EXAMPLE:0443/", "https://topology.example"),
    ("https://TOPOLOGY.EXAMPLE:08443", "https://topology.example:8443"),
    ("http://LOCALHOST:80", "http://localhost"),
    ("http://127.0.0.1:080/", "http://127.0.0.1"),
    ("http://LOCALHOST:8000", "http://localhost:8000"),
    ("http://[0:0:0:0:0:0:0:1]:80", "http://[::1]"),
    ("http://[::1]:8000", "http://[::1]:8000"),
    ("https://[2001:0DB8:0:0:1:0:0:1]:443", "https://[2001:db8::1:0:0:1]"),
    ("https://[2001:0DB8:0:0:1:0:0:1]:8443", "https://[2001:db8::1:0:0:1]:8443"),
    ("https://[::FFFF:192.0.2.128]:443", "https://[::ffff:c000:280]"),
    ("https://topology.éxample", "https://topology.xn--xample-9ua"),
)
