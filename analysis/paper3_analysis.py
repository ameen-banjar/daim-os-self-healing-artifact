#!/usr/bin/env python3
"""Generate Paper 3 (Self-Healing DAIM-OS) figures from real Stage 3 data and
from the actual hold-down/flapping-link unit test, in the same hand-drawn PIL
house style as experiments/analysis/paper1_analysis.py (box/h_arrow/v_arrow
helpers). Every number plotted here is read from the real raw CSV or computed
by actually importing and running the same functions the unit test in
experiments/network/test_daim_link_agent.py exercises -- nothing here is
invented to make a nicer-looking chart.

Font sizes here are deliberately large relative to canvas width (compare to
experiments/analysis/paper1_analysis.py): these figures are inserted at a
fixed 6.3in width in the submission docx, and a canvas rendered at
"print resolution" (~300 DPI) with modest font-pixel sizes reads as tiny once
placed on the page. Every canvas here targets roughly 150-170 effective DPI
at 6.3in insertion, so figure text prints close to the ~11pt body text size.
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

def styled_segment(draw, a, b, style="solid", width=5, color="#111111"):
    x0, y0 = a
    x1, y1 = b
    length = max(1.0, ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5)
    pattern = [length] if style == "solid" else [30, 15]
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


def text_block_size(draw, text, font):
    lines = text.split("\n")
    line_h = font.size + 8
    widths = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        widths.append(bbox[2] - bbox[0])
    return max(widths), line_h * len(lines)


def box(draw, xy, text, font, fill="#F1F1F1", outline="#333333", text_color="#1F2933"):
    x0, y0, x1, y1 = xy
    draw.rectangle(xy, fill=fill, outline=outline, width=3)
    lines = text.split("\n")
    line_h = font.size + 8
    total_h = line_h * len(lines)
    ty = y0 + ((y1 - y0) - total_h) / 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        draw.text((x0 + ((x1 - x0) - tw) / 2, ty), line, fill=text_color, font=font)
        ty += line_h


def sized_box(draw, cx, cy, text, font, pad_x=60, pad_y=36, **kwargs):
    """Box centred at (cx, cy), sized to fit `text` at `font` plus padding.
    Returns the (x0, y0, x1, y1) box extent actually used."""
    tw, th = text_block_size(draw, text, font)
    xy = (cx - tw / 2 - pad_x, cy - th / 2 - pad_y, cx + tw / 2 + pad_x, cy + th / 2 + pad_y)
    box(draw, xy, text, font, **kwargs)
    return xy


def two_tier_box(draw, cx, cy, title, sub, title_font, sub_font, pad_x=70, pad_y=34, gap=14,
                  fill="#F1F1F1", outline="#333333", title_color="#1F2933", sub_color="#5B6570"):
    """Box centred at (cx, cy) with a bold title line (title_font) and a
    smaller subtitle line (sub_font) stacked below it, each measured and
    centred independently so the two font sizes never overlap."""
    tw, th = text_block_size(draw, title, title_font)
    sw, sh = text_block_size(draw, sub, sub_font)
    content_w = max(tw, sw)
    content_h = th + gap + sh
    xy = (cx - content_w / 2 - pad_x, cy - content_h / 2 - pad_y,
          cx + content_w / 2 + pad_x, cy + content_h / 2 + pad_y)
    draw.rectangle(xy, fill=fill, outline=outline, width=3)
    ty = cy - content_h / 2
    draw.text((cx - tw / 2, ty), title, fill=title_color, font=title_font)
    draw.text((cx - sw / 2, ty + th + gap), sub, fill=sub_color, font=sub_font)
    return xy


def h_arrow(draw, x0, x1, y, label, font, color="#333333", dashed=False, label_dy=-34, width=4):
    if dashed:
        step = 18
        xx = x0
        while xx < x1 - step:
            draw.line((xx, y, xx + step * 0.6, y), fill=color, width=width - 1)
            xx += step
    else:
        draw.line((x0, y, x1, y), fill=color, width=width)
    direction = 1 if x1 >= x0 else -1
    ax = x1
    draw.polygon([(ax, y), (ax - 18 * direction, y - 9), (ax - 18 * direction, y + 9)], fill=color)
    if label:
        bbox = draw.textbbox((0, 0), label, font=font)
        tw = bbox[2] - bbox[0]
        tx = min(x0, x1) + abs(x1 - x0) / 2 - tw / 2
        ty = y + label_dy
        draw.rectangle((tx - 10, ty - 4, tx + tw + 10, ty + font.size + 6), fill="white")
        draw.text((tx, ty), label, fill=color, font=font)


def v_arrow(draw, x, y0, y1, label, font, color="#333333", label_dx=14, width=4):
    draw.line((x, y0, x, y1), fill=color, width=width)
    direction = 1 if y1 >= y0 else -1
    ay = y1
    draw.polygon([(x, ay), (x - 9, ay - 18 * direction), (x + 9, ay - 18 * direction)], fill=color)
    if label:
        draw.text((x + label_dx, min(y0, y1) + abs(y1 - y0) / 2 - 12), label, fill=color, font=font)


# ------------------------------------------------------------- Figure 1 ----

def draw_architecture(path):
    width, height = 2200, 1500
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.load_default(size=54)
    font = ImageFont.load_default(size=42)
    small = ImageFont.load_default(size=36)

    draw.text((24, 24), "Self-healing DAIM-OS agent: component architecture", fill="#111111", font=title_font)

    ovsdb = sized_box(draw, 1000, 200, "ovsdb-server\n(Interface table)", font, fill="#EAF2FB", outline="#0072B2")
    monitor = sized_box(draw, 1000, 420, "ovsdb-client monitor\n(child process)", font, fill="#EAF2FB", outline="#0072B2")
    hosts = sized_box(draw, 245, 780, "Hosts\nh1 (src), h2 (dst)", font, fill="#EAF2FB", outline="#0072B2")
    agent = two_tier_box(
        draw, 1000, 780, "daim_link_agent.py", "watcher + BFS + hold-down state machine",
        font, small, pad_x=55, pad_y=46, fill="#FFF0E6", outline="#D55E00",
        title_color="#7a3200", sub_color="#8a3b00",
    )
    switches = sized_box(draw, 1870, 780, "OVS switches\ns1 - s2 - s3 - s4", font, fill="#EAF2FB", outline="#0072B2")
    adapter = sized_box(draw, 1000, 1160, "daim_ovs_flow adapter", font, fill="#FFF0E6", outline="#D55E00")

    v_arrow(draw, 1000, ovsdb[3], monitor[1], "", small)
    draw.text((1030, (ovsdb[3] + monitor[1]) / 2 - 20), "push notification (link_state change)", fill="#333333", font=small)

    v_arrow(draw, 1000, monitor[3], agent[1], "", small)
    draw.text((1030, (monitor[3] + agent[1]) / 2 - 20), "stdout JSON line", fill="#333333", font=small)

    h_arrow(draw, hosts[2], agent[0], 780, "", small)
    draw.text((hosts[2] + 10, 596), "declared\ntopology graph", fill="#333333", font=small)

    dash_y = 810
    gap_mid = (agent[2] + switches[0]) / 2
    styled_segment(draw, (agent[2] + 15, dash_y), (switches[0] - 15, dash_y), "dashed", 4, "#888888")
    draw.polygon([(switches[0] - 15, dash_y), (switches[0] - 33, dash_y - 9), (switches[0] - 33, dash_y + 9)], fill="#888888")
    for li, line in enumerate(("data plane", "(not traversed by agent)")):
        lb = draw.textbbox((0, 0), line, font=small)
        lw = lb[2] - lb[0]
        draw.text((gap_mid - lw / 2, 580 + li * 44), line, fill="#888888", font=small)

    v_arrow(draw, 1000, agent[3], adapter[1], "", small)
    draw.text((1030, (agent[3] + adapter[1]) / 2 - 20), "add / delete", fill="#333333", font=small)

    adapter_cy = (adapter[1] + adapter[3]) / 2
    switches_cy = (switches[1] + switches[3]) / 2
    x_return = switches[2] + 70
    draw.line((adapter[2], adapter_cy, x_return, adapter_cy), fill="#111111", width=5)
    draw.line((x_return, adapter_cy, x_return, switches_cy), fill="#111111", width=5)
    h_arrow(draw, x_return, switches[2], switches_cy, "", small, color="#111111")
    draw.text((x_return - 200, (adapter_cy + switches_cy) / 2 - 24), "OpenFlow\nFlow-Mod", fill="#111111", font=small)

    draw.text(
        (24, 1360),
        "Blue: existing OVSDB/OVS/host components. Orange: this paper's new\nprocess and its state machine.",
        fill="#333333", font=small,
    )
    draw.text(
        (24, 1440),
        "The agent is a single Python process -- watcher, BFS engine, and hold-down state share one event loop.",
        fill="#333333", font=small,
    )
    image.save(path)


# ------------------------------------------------------------- Figure 2 ----

def draw_sequence(path):
    width, height = 2000, 1350
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.load_default(size=50)
    font = ImageFont.load_default(size=36)
    small = ImageFont.load_default(size=33)

    draw.text((24, 24), "Link failure to repaired path: message sequence", fill="#111111", font=title_font)

    actors = [
        ("OVS switch\n(s1-eth2)", 170),
        ("ovsdb-server", 540),
        ("ovsdb-client\nmonitor", 920),
        ("daim_link_agent\n(watcher + BFS)", 1350),
        ("daim_ovs_flow\nadapter", 1780),
    ]
    top_y = 130
    bottom_y = 1230
    for name, x in actors:
        tw, th = text_block_size(draw, name, font)
        box(draw, (x - tw / 2 - 26, top_y, x + tw / 2 + 26, top_y + th + 30), name, font, fill="#EAF2FB", outline="#0072B2")
        draw.line((x, top_y + th + 24, x, bottom_y), fill="#BBBBBB", width=2)

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
    y = 310
    step_h = 118
    for src, dst, label, dashed in steps:
        x0, x1 = xs[src], xs[dst]
        if x0 == x1:
            draw.arc((x0 - 50, y - 26, x0 + 50, y + 26), start=300, end=240, fill="#111111", width=4)
            draw.text((x0 + 62, y - 16), label, fill="#111111", font=small)
        else:
            h_arrow(draw, x0, x1, y, label, small, color="#111111", dashed=dashed, label_dy=-28)
        y += step_h

    draw.text(
        (24, 1275),
        f"Dashed: internal state update, not a message on the wire. Hold-down window = {HOLD_DOWN_SECONDS:.1f}s by default.",
        fill="#555555", font=small,
    )
    image.save(path)


# ------------------------------------------------------------- Figure 3 ----

def draw_topology(path):
    width, height = 1550, 950
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.load_default(size=46)
    font = ImageFont.load_default(size=38)
    small = ImageFont.load_default(size=34)

    draw.text((24, 24), "Diamond topology used for all Paper 3 measurements", fill="#111111", font=title_font)

    cy = 500
    h1 = sized_box(draw, 110, cy, "h1\n(source)", font, fill="#EAF2FB", outline="#0072B2")
    s1 = sized_box(draw, 400, cy, "s1", font, fill="#EAF2FB", outline="#0072B2")
    s2 = sized_box(draw, 775, 260, "s2", font, fill="#FFF0E6", outline="#D55E00")
    s3 = sized_box(draw, 775, 740, "s3", font, fill="#EAF2FB", outline="#0072B2")
    s4 = sized_box(draw, 1150, cy, "s4", font, fill="#EAF2FB", outline="#0072B2")
    h2 = sized_box(draw, 1440, cy, "h2\n(dest)", font, fill="#EAF2FB", outline="#0072B2")

    h_arrow(draw, h1[2], s1[0], cy, "", small)
    h_arrow(draw, s4[2], h2[0], cy, "", small)

    styled_segment(draw, (s1[2], cy), (s2[0], s2[1] + (s2[3] - s2[1]) / 2), "solid", 6, "#D55E00")
    styled_segment(draw, (s2[2], s2[1] + (s2[3] - s2[1]) / 2), (s4[0], cy), "solid", 6, "#D55E00")
    draw.text((360, 100), "primary path (s1-s2-s4)\ninjected failure: s1-eth2 / s2-eth1", fill="#D55E00", font=small)

    styled_segment(draw, (s1[2], cy), (s3[0], s3[1] + (s3[3] - s3[1]) / 2), "dashed", 5, "#0072B2")
    styled_segment(draw, (s3[2], s3[1] + (s3[3] - s3[1]) / 2), (s4[0], cy), "dashed", 5, "#0072B2")
    draw.text((420, 795), "alternate path (s1-s3-s4), installed after repair", fill="#0072B2", font=small)

    draw.text(
        (24, 880),
        "4 switches, 5 links. This is the only topology measured for Paper 3 so far.",
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

    width, height = 1750, 1150
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.load_default(size=44)
    font = ImageFont.load_default(size=38)
    small = ImageFont.load_default(size=34)

    draw.text((24, 24), "Autonomous link-failure repair:", fill="#111111", font=title_font)
    draw.text((24, 76), "5 measured repetitions (Stage 3 raw data)", fill="#111111", font=title_font)
    draw.text((24, 148), "milliseconds", fill="#222222", font=small)

    left, top, right, bottom = 170, 200, 1660, 870
    draw.line((left, top, left, bottom), fill="#222222", width=3)
    draw.line((left, bottom, right, bottom), fill="#222222", width=3)
    ymax = 200.0
    for tick in range(0, 201, 25):
        y = bottom - (tick / ymax) * (bottom - top)
        draw.line((left - 10, y, right, y), fill="#dddddd", width=1)
        draw.text((70, y - 16), str(tick), fill="#222222", font=small)

    n = len(rows)
    group_w = (right - left) / n
    bar_w = group_w * 0.28
    for i, row in enumerate(rows):
        cx = left + group_w * (i + 0.5)
        det_h = (row["detection_ms"] / ymax) * (bottom - top)
        rep_h = (row["repair_ms"] / ymax) * (bottom - top)
        draw.rectangle((cx - bar_w - 5, bottom - det_h, cx - 5, bottom), fill="#0072B2")
        draw.rectangle((cx + 5, bottom - rep_h, cx + 5 + bar_w, bottom), fill="#D55E00")
        draw.text((cx - 30, bottom + 18), f"rep {row['rep']}", fill="#222222", font=small)
        draw.text((cx - 34, bottom - rep_h - 44), f"{row['repair_ms']:.0f}", fill="#D55E00", font=small)
        draw.text((cx - bar_w - 44, bottom - det_h - 44), f"{row['detection_ms']:.1f}", fill="#0072B2", font=small)

    draw.rectangle((1280, 210, 1305, 235), fill="#0072B2")
    draw.text((1315, 205), "detection time", fill="#222222", font=small)
    draw.rectangle((1280, 260, 1305, 285), fill="#D55E00")
    draw.text((1315, 255), "repair-action time", fill="#222222", font=small)

    mean_det = sum(r["detection_ms"] for r in rows) / n
    mean_rep = sum(r["repair_ms"] for r in rows) / n
    mean_loss = sum(r["loss_pct"] for r in rows) / n
    draw.text(
        (170, 940),
        f"Mean detection {mean_det:.2f} ms, mean repair action {mean_rep:.2f} ms,\n"
        f"mean packet loss {mean_loss:.2f}% (n=5, single diamond topology, single link failure).",
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
    "noop": "no-op (BFS re-run,\npath unchanged)",
}


def draw_holddown_timeline(path):
    with_hd = run_flap_sequence(hold_down_seconds=HOLD_DOWN_SECONDS)
    without_hd = run_flap_sequence(hold_down_seconds=0.0)
    times = [t for t, _ in FLAP_EVENTS]

    width, height = 1750, 1250
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.load_default(size=42)
    font = ImageFont.load_default(size=36)
    small = ImageFont.load_default(size=32)

    draw.text(
        (24, 24),
        "Identical flapping-link event sequence,",
        fill="#111111", font=title_font,
    )
    draw.text(
        (24, 74),
        "real output of decide_link_event()",
        fill="#111111", font=title_font,
    )
    draw.text(
        (24, 132),
        "(test_daim_link_agent.py::test_holddown_suppresses_flapping)",
        fill="#666666", font=small,
    )

    left, right = 220, 1680
    tmax = max(times) + 0.4

    def x_of(t):
        return left + (t / tmax) * (right - left)

    rows = [
        ("hold-down disabled (window=0.0s)", without_hd, 380),
        (f"hold-down enabled (window={HOLD_DOWN_SECONDS:.1f}s)", with_hd, 660),
    ]
    for label, actions, y in rows:
        draw.text((24, y - 130), label, fill="#111111", font=font)
        draw.line((left, y, right, y), fill="#cccccc", width=2)
        prev_x = None
        stagger = 0
        for (t, state), action in zip(FLAP_EVENTS, actions):
            x = x_of(t)
            color = ACTION_COLOR[action]
            draw.ellipse((x - 16, y - 16, x + 16, y + 16), fill=color, outline="#111111", width=2)
            stagger = (stagger + 1) if (prev_x is not None and x - prev_x < 60) else 0
            draw.text((x - 20, y + 28 + stagger * 34), state, fill="#333333", font=small)
            prev_x = x

    prev_x = None
    stagger = 0
    for t in times:
        x = x_of(t)
        draw.line((x, 860, x, 878), fill="#888888", width=2)
        stagger = (stagger + 1) if (prev_x is not None and x - prev_x < 70) else 0
        draw.text((x - 20, 884 + stagger * 38), f"{t:.1f}s", fill="#333333", font=small)
        prev_x = x

    ly = 1060
    lx = 24
    col_w = [420, 400, 460, 460]
    for (action, color), cw in zip(ACTION_COLOR.items(), col_w):
        draw.ellipse((lx, ly, lx + 26, ly + 26), fill=color, outline="#111111", width=2)
        draw.text((lx + 36, ly - 4), ACTION_LABEL[action], fill="#222222", font=small)
        lx += cw
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
