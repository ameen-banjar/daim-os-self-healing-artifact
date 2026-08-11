#!/usr/bin/env python3
"""Generate Paper 3 (Self-Healing DAIM-OS) figures from real Stage 3 data and
from the actual hold-down/flapping-link unit test, in the same hand-drawn PIL
house style as experiments/analysis/paper1_analysis.py (box/h_arrow/v_arrow
helpers). Every number plotted here is read from the real raw CSV or computed
by actually importing and running the same functions the unit test in
experiments/network/test_daim_link_agent.py exercises -- nothing here is
invented to make a nicer-looking chart.
"""
import csv
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
NETWORK_DIR = ROOT / "network"
RAW = ROOT / "results/network/stage3_autonomous_agent_raw.csv"
OUT = ROOT / "results/paper3"
OUT.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(NETWORK_DIR))
from daim_link_agent import HOLD_DOWN_SECONDS  # noqa: E402
from test_daim_link_agent import FLAP_EVENTS, run_flap_sequence  # noqa: E402


# ---------------------------------------------------------------- helpers --
# Reused verbatim from paper1_analysis.py so Paper 3's figures share the
# same visual language as Papers 1 and 2.

def styled_segment(draw, a, b, style="solid", width=4, color="#111111"):
    x0, y0 = a
    x1, y1 = b
    length = max(1.0, ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5)
    pattern = [length] if style == "solid" else [24, 12]
    pos = 0.0
    pi = 0
    on = True
    while pos < length:
        end = min(length, pos + pattern[pi % len(pattern)])
        if on:
            draw.line((x0 + (x1 - x0) * pos / length, y0 + (y1 - y0) * pos / length,
                       x0 + (x1 - x0) * end / length, y0 + (y1 - y0) * end / length),
                      fill=color, width=width)
        pos = end
        pi += 1
        on = not on


def box(draw, xy, text, font, fill="#F1F1F1", outline="#333333", text_color="#1F2933"):
    x0, y0, x1, y1 = xy
    draw.rectangle(xy, fill=fill, outline=outline, width=3)
    lines = text.split("\n")
    line_h = font.size + 6
    total_h = line_h * len(lines)
    ty = y0 + ((y1 - y0) - total_h) / 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        draw.text((x0 + ((x1 - x0) - tw) / 2, ty), line, fill=text_color, font=font)
        ty += line_h


def h_arrow(draw, x0, x1, y, label, font, color="#333333", dashed=False, label_dy=-26):
    if dashed:
        step = 14
        xx = x0
        while xx < x1 - step:
            draw.line((xx, y, xx + step * 0.6, y), fill=color, width=2)
            xx += step
    else:
        draw.line((x0, y, x1, y), fill=color, width=3)
    direction = 1 if x1 >= x0 else -1
    ax = x1
    draw.polygon([(ax, y), (ax - 14 * direction, y - 7), (ax - 14 * direction, y + 7)], fill=color)
    if label:
        bbox = draw.textbbox((0, 0), label, font=font)
        tw = bbox[2] - bbox[0]
        tx = min(x0, x1) + abs(x1 - x0) / 2 - tw / 2
        ty = y + label_dy
        draw.rectangle((tx - 8, ty - 3, tx + tw + 8, ty + font.size + 4), fill="white")
        draw.text((tx, ty), label, fill=color, font=font)


def v_arrow(draw, x, y0, y1, label, font, color="#333333", label_dx=10):
    draw.line((x, y0, x, y1), fill=color, width=3)
    direction = 1 if y1 >= y0 else -1
    ay = y1
    draw.polygon([(x, ay), (x - 7, ay - 14 * direction), (x + 7, ay - 14 * direction)], fill=color)
    if label:
        draw.text((x + label_dx, min(y0, y1) + abs(y1 - y0) / 2 - 10), label, fill=color, font=font)


# ------------------------------------------------------------- Figure 1 ----

def draw_architecture(path):
    width, height = 1900, 1150
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.load_default(size=42)
    font = ImageFont.load_default(size=29)
    small = ImageFont.load_default(size=26)

    draw.text((20, 20), "Self-healing DAIM-OS agent: component architecture", fill="#111111", font=title_font)

    ovsdb = (790, 110, 1110, 200)
    monitor = (790, 290, 1110, 380)
    hosts = (60, 470, 430, 570)
    agent = (700, 470, 1200, 610)
    switches = (1500, 470, 1850, 570)
    adapter = (790, 760, 1110, 870)

    box(draw, ovsdb, "ovsdb-server\n(Interface table)", font, fill="#EAF2FB", outline="#0072B2")
    box(draw, monitor, "ovsdb-client monitor\n(child process)", font, fill="#EAF2FB", outline="#0072B2")
    box(draw, hosts, "Hosts\nh1 (src), h2 (dst)", font, fill="#EAF2FB", outline="#0072B2")
    box(draw, agent, "daim_link_agent.py", font, fill="#FFF0E6", outline="#D55E00")
    draw.text((agent[0] + 40, agent[1] + 70), "watcher + BFS + hold-down state machine", fill="#8a3b00", font=small)
    box(draw, switches, "OVS switches\ns1 - s2 - s3 - s4", font, fill="#EAF2FB", outline="#0072B2")
    box(draw, adapter, "daim_ovs_flow adapter", font, fill="#FFF0E6", outline="#D55E00")

    v_arrow(draw, 950, ovsdb[3], monitor[1], "", small)
    draw.text((970, 225), "push notification (link_state change)", fill="#333333", font=small)
    v_arrow(draw, 950, monitor[3], agent[1] - 20, "", small)
    draw.line((950, agent[1] - 20, 950, agent[1]), fill="#333333", width=3)
    draw.text((970, 405), "stdout JSON line", fill="#333333", font=small)

    h_arrow(draw, hosts[2], agent[0], 520, "", small)
    draw.text((450, 480), "declared topology graph", fill="#333333", font=small)

    styled_segment(draw, (agent[2], 494), (switches[0], 494), "dashed", 3, "#888888")
    draw.polygon([(switches[0], 494), (switches[0] - 14, 487), (switches[0] - 14, 501)], fill="#888888")
    draw.text((1240, 420), "data plane (not\ntraversed by agent)", fill="#888888", font=small)

    v_arrow(draw, 950, agent[3], adapter[1], "", small)
    draw.text((970, 660), "add / delete", fill="#333333", font=small)

    x_return = 1700
    draw.line((adapter[2], 815, x_return, 815), fill="#111111", width=4)
    draw.line((x_return, 815, x_return, 520), fill="#111111", width=4)
    v_arrow(draw, x_return, 815, switches[3], "", small, color="#111111")
    draw.text((1720, 650), "OpenFlow\nFlow-Mod", fill="#111111", font=small)

    draw.text(
        (30, 1010),
        "Blue: existing OVSDB/OVS/host components. Orange: this paper's new process and its state machine.",
        fill="#333333", font=small,
    )
    draw.text(
        (30, 1045),
        "The agent is a single Python process -- watcher, BFS engine, and hold-down state share one event loop.",
        fill="#333333", font=small,
    )
    image.save(path)


# ------------------------------------------------------------- Figure 2 ----

def draw_sequence(path):
    width, height = 1700, 1020
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.load_default(size=40)
    font = ImageFont.load_default(size=27)
    small = ImageFont.load_default(size=26)

    draw.text((20, 20), "Link failure to repaired path: message sequence", fill="#111111", font=title_font)

    actors = [
        ("OVS switch\n(s1-eth2)", 150),
        ("ovsdb-server", 460),
        ("ovsdb-client\nmonitor", 780),
        ("daim_link_agent\n(watcher + BFS)", 1130),
        ("daim_ovs_flow\nadapter", 1480),
    ]
    top_y = 110
    bottom_y = 930
    for name, x in actors:
        box(draw, (x - 120, top_y, x + 120, top_y + 78), name, font, fill="#EAF2FB", outline="#0072B2")
        draw.line((x, top_y + 70, x, bottom_y), fill="#BBBBBB", width=2)

    xs = {name: x for name, x in actors}
    steps = [
        ("OVS switch\n(s1-eth2)", "ovsdb-server", "1  link_state -> down", False),
        ("ovsdb-server", "ovsdb-client\nmonitor", "2  monitor push update", False),
        ("ovsdb-client\nmonitor", "daim_link_agent\n(watcher + BFS)", "3  JSON row on stdout", False),
        ("daim_link_agent\n(watcher + BFS)", "daim_link_agent\n(watcher + BFS)", "4  BFS over declared graph", False),
        ("daim_link_agent\n(watcher + BFS)", "daim_ovs_flow\nadapter", "5  delete (old path)", False),
        ("daim_link_agent\n(watcher + BFS)", "daim_ovs_flow\nadapter", "6  add (new path)", False),
        ("daim_ovs_flow\nadapter", "OVS switch\n(s1-eth2)", "7  Flow-Mod installs rule", False),
        ("daim_link_agent\n(watcher + BFS)", "daim_link_agent\n(watcher + BFS)", "8  start hold-down window", True),
    ]
    y = 250
    step_h = 92
    for src, dst, label, dashed in steps:
        x0, x1 = xs[src], xs[dst]
        if x0 == x1:
            # self-transition (agent-internal step): small loop-back arrow.
            draw.arc((x0 - 40, y - 20, x0 + 40, y + 20), start=300, end=240, fill="#111111", width=3)
            draw.text((x0 + 50, y - 12), label, fill="#111111", font=small)
        else:
            h_arrow(draw, x0, x1, y, label, small, color="#111111", dashed=dashed, label_dy=-20)
        y += step_h

    draw.text(
        (20, 975),
        f"Dashed: internal state update, not a message on the wire. Hold-down window = {HOLD_DOWN_SECONDS:.1f}s by default.",
        fill="#555555", font=small,
    )
    image.save(path)


# ------------------------------------------------------------- Figure 3 ----

def draw_topology(path):
    width, height = 1400, 780
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.load_default(size=38)
    font = ImageFont.load_default(size=30)
    small = ImageFont.load_default(size=26)

    draw.text((20, 20), "Diamond topology used for all Paper 3 measurements", fill="#111111", font=title_font)

    h1 = (60, 360, 200, 440)
    s1 = (330, 360, 470, 440)
    s2 = (620, 190, 760, 270)
    s3 = (620, 530, 760, 610)
    s4 = (910, 360, 1050, 440)
    h2 = (1180, 360, 1320, 440)

    box(draw, h1, "h1\n(source)", font, fill="#EAF2FB", outline="#0072B2")
    box(draw, s1, "s1", font, fill="#EAF2FB", outline="#0072B2")
    box(draw, s2, "s2", font, fill="#FFF0E6", outline="#D55E00")
    box(draw, s3, "s3", font, fill="#EAF2FB", outline="#0072B2")
    box(draw, s4, "s4", font, fill="#EAF2FB", outline="#0072B2")
    box(draw, h2, "h2\n(dest)", font, fill="#EAF2FB", outline="#0072B2")

    h_arrow(draw, h1[2], s1[0], 400, "", small)
    h_arrow(draw, s4[2], h2[0], 400, "", small)

    styled_segment(draw, (s1[2], 400), (s2[0], 230), "solid", 5, "#D55E00")
    styled_segment(draw, (s2[2], 230), (s4[0], 400), "solid", 5, "#D55E00")
    draw.text((430, 100), "primary path (s1-s2-s4)\ninjected failure: s1-eth2 / s2-eth1", fill="#D55E00", font=small)

    styled_segment(draw, (s1[2], 400), (s3[0], 570), "dashed", 4, "#0072B2")
    styled_segment(draw, (s3[2], 570), (s4[0], 400), "dashed", 4, "#0072B2")
    draw.text((470, 620), "alternate path (s1-s3-s4), installed after repair", fill="#0072B2", font=small)

    draw.text(
        (20, 710),
        "4 switches, 5 links. This is the only topology measured for Paper 3 so far (Section on Limitations).",
        fill="#333333", font=small,
    )
    image.save(path)


# ------------------------------------------------------------- Figure 4 ----

def draw_recovery_chart(path):
    rows = []
    with open(RAW, newline="") as f:
        for row in csv.DictReader(f):
            rows.append({
                "rep": int(row["repetition"]),
                "detection_ms": float(row["failure_to_detection_us"]) / 1000.0,
                "repair_ms": float(row["repair_action_us"]) / 1000.0,
                "loss_pct": float(row["packet_loss_pct"]),
            })
    rows.sort(key=lambda r: r["rep"])

    width, height = 1600, 920
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.load_default(size=36)
    font = ImageFont.load_default(size=30)
    small = ImageFont.load_default(size=26)

    draw.text((20, 20), "Autonomous link-failure repair: 5 measured repetitions (Stage 3 raw data)", fill="#111111", font=title_font)
    draw.text((20, 75), "milliseconds", fill="#222222", font=small)

    left, top, right, bottom = 140, 130, 1540, 720
    draw.line((left, top, left, bottom), fill="#222222", width=3)
    draw.line((left, bottom, right, bottom), fill="#222222", width=3)
    ymax = 200.0
    for tick in range(0, 201, 25):
        y = bottom - (tick / ymax) * (bottom - top)
        draw.line((left - 8, y, right, y), fill="#dddddd", width=1)
        draw.text((60, y - 12), str(tick), fill="#222222", font=small)

    n = len(rows)
    group_w = (right - left) / n
    bar_w = group_w * 0.28
    for i, row in enumerate(rows):
        cx = left + group_w * (i + 0.5)
        det_h = (row["detection_ms"] / ymax) * (bottom - top)
        rep_h = (row["repair_ms"] / ymax) * (bottom - top)
        draw.rectangle((cx - bar_w - 4, bottom - det_h, cx - 4, bottom), fill="#0072B2")
        draw.rectangle((cx + 4, bottom - rep_h, cx + 4 + bar_w, bottom), fill="#D55E00")
        draw.text((cx - 14, bottom + 15), f"rep {row['rep']}", fill="#222222", font=small)
        draw.text((cx - 30, bottom - rep_h - 34), f"{row['repair_ms']:.0f}", fill="#D55E00", font=small)
        draw.text((cx - bar_w - 34, bottom - det_h - 34), f"{row['detection_ms']:.1f}", fill="#0072B2", font=small)

    draw.rectangle((1180, 140, 1200, 160), fill="#0072B2")
    draw.text((1210, 135), "detection time", fill="#222222", font=small)
    draw.rectangle((1180, 180, 1200, 200), fill="#D55E00")
    draw.text((1210, 175), "repair-action time", fill="#222222", font=small)

    mean_det = sum(r["detection_ms"] for r in rows) / n
    mean_rep = sum(r["repair_ms"] for r in rows) / n
    mean_loss = sum(r["loss_pct"] for r in rows) / n
    draw.text(
        (140, 790),
        f"Mean detection {mean_det:.2f} ms, mean repair action {mean_rep:.2f} ms, "
        f"mean packet loss {mean_loss:.2f}%\n(n=5, single diamond topology, single link failure).",
        fill="#333333", font=small,
    )
    image.save(path)


# ------------------------------------------------------------- Figure 5 ----

ACTION_COLOR = {
    "repair": "#D55E00",
    "suppressed": "#999999",
    "recovered": "#0072B2",
    "noop": "#2E9E5B",
}
ACTION_LABEL = {
    "repair": "repair (BFS + flow install)",
    "suppressed": "suppressed (hold-down)",
    "recovered": "recovered (state cleared)",
    "noop": "no-op (BFS re-run, path unchanged)",
}


def draw_holddown_timeline(path):
    with_hd = run_flap_sequence(hold_down_seconds=HOLD_DOWN_SECONDS)
    without_hd = run_flap_sequence(hold_down_seconds=0.0)
    times = [t for t, _ in FLAP_EVENTS]

    width, height = 1500, 820
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.load_default(size=36)
    font = ImageFont.load_default(size=28)
    small = ImageFont.load_default(size=24)

    draw.text(
        (20, 20),
        "Identical flapping-link event sequence, real output of decide_link_event()",
        fill="#111111", font=title_font,
    )
    draw.text(
        (20, 65),
        "(experiments/network/test_daim_link_agent.py::test_holddown_suppresses_flapping)",
        fill="#666666", font=small,
    )

    left, right = 180, 1440
    tmax = max(times) + 0.4

    def x_of(t):
        return left + (t / tmax) * (right - left)

    rows = [
        ("hold-down disabled (window=0.0s)", without_hd, 280),
        (f"hold-down enabled (window={HOLD_DOWN_SECONDS:.1f}s)", with_hd, 520),
    ]
    for label, actions, y in rows:
        draw.text((20, y - 100), label, fill="#111111", font=font)
        draw.line((left, y, right, y), fill="#cccccc", width=2)
        prev_x = None
        stagger = 0
        for (t, state), action in zip(FLAP_EVENTS, actions):
            x = x_of(t)
            color = ACTION_COLOR[action]
            draw.ellipse((x - 12, y - 12, x + 12, y + 12), fill=color, outline="#111111", width=2)
            stagger = (stagger + 1) if (prev_x is not None and x - prev_x < 45) else 0
            draw.text((x - 16, y + 22 + stagger * 26), state, fill="#333333", font=small)
            prev_x = x

    for t in times:
        x = x_of(t)
        draw.line((x, 680, x, 695), fill="#888888", width=2)
        draw.text((x - 15, 700), f"{t:.1f}s", fill="#333333", font=small)

    ly = 760
    lx = 40
    for action, color in ACTION_COLOR.items():
        draw.ellipse((lx, ly, lx + 20, ly + 20), fill=color, outline="#111111", width=2)
        draw.text((lx + 28, ly - 2), ACTION_LABEL[action], fill="#222222", font=small)
        lx += 330
    image.save(path)


def main():
    draw_architecture(OUT / "paper3_architecture.png")
    draw_sequence(OUT / "paper3_sequence.png")
    draw_topology(OUT / "paper3_topology.png")
    draw_recovery_chart(OUT / "paper3_recovery_timeline.png")
    draw_holddown_timeline(OUT / "paper3_holddown_timeline.png")
    print("Wrote:")
    for name in ("paper3_architecture.png", "paper3_sequence.png", "paper3_topology.png",
                 "paper3_recovery_timeline.png", "paper3_holddown_timeline.png"):
        print(" ", OUT / name)


if __name__ == "__main__":
    main()
