#!/usr/bin/env python3
"""Regression test for a real bug found by code review in
stage3_startup_already_down.py: `parse_ping_loss_pct()` replaced a
substring check, `"0% packet loss" not in ping_output`, that silently
misclassified a 100%-loss run as loss-free, because "100% packet loss"
itself contains "0% packet loss" as a substring (the trailing "0%" of
"100%"). This is exactly the string a real `ping` command produced against
the pre-fix agent in the startup-already-down scenario, so this was not a
hypothetical edge case -- it affected a real stored result
(`stage3_startup_already_down_result_pre_fix.json`'s `ping_had_loss` field
was `false` despite the raw ping output showing 100% loss)."""
from stage3_startup_already_down import parse_ping_loss_pct

REAL_100_PCT_LOSS_OUTPUT = (
    "PING 10.0.0.2 (10.0.0.2) 56(84) bytes of data.\n"
    "From 10.0.0.1 icmp_seq=1 Destination Host Unreachable\n"
    "\n"
    "--- 10.0.0.2 ping statistics ---\n"
    "20 packets transmitted, 0 received, +10 errors, 100% packet loss, time 4070ms\n"
    "pipe 11\n"
)

REAL_0_PCT_LOSS_OUTPUT = (
    "PING 10.0.0.2 (10.0.0.2) 56(84) bytes of data.\n"
    "64 bytes from 10.0.0.2: icmp_seq=1 ttl=64 time=0.105 ms\n"
    "\n"
    "--- 10.0.0.2 ping statistics ---\n"
    "20 packets transmitted, 20 received, 0% packet loss, time 1546ms\n"
    "rtt min/avg/max/mdev = 0.032/0.173/1.128/0.225 ms\n"
)


def test_parse_ping_loss_pct_full_loss():
    assert parse_ping_loss_pct(REAL_100_PCT_LOSS_OUTPUT) == 100.0, (
        "the substring-check bug this replaces reported 100%-loss output "
        "as loss-free, because '100% packet loss' contains '0% packet "
        "loss' as a substring"
    )
    print("parse_ping_loss_pct 100%-loss regression test: PASS")


def test_parse_ping_loss_pct_no_loss():
    assert parse_ping_loss_pct(REAL_0_PCT_LOSS_OUTPUT) == 0.0
    print("parse_ping_loss_pct 0%-loss regression test: PASS")


def main():
    test_parse_ping_loss_pct_full_loss()
    test_parse_ping_loss_pct_no_loss()


if __name__ == "__main__":
    main()
