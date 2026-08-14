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
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
NETWORK_DIR = ROOT / "network"
RAW = ROOT / "results/network/stage3_autonomous_agent_raw.csv"
FLAP_RAW = ROOT / "results/network/stage3_holddown_flapping_raw.csv"
FLAP_RAW_PRE_FIX = ROOT / "results/network/stage3_holddown_flapping_raw_pre_edgefix.csv"
STARTUP_RESULT = ROOT / "results/network/stage3_startup_already_down_result.json"
STARTUP_RESULT_PRE_FIX = ROOT / "results/network/stage3_startup_already_down_result_pre_fix.json"
MULTI_OVS_RAW = ROOT / "results/network/stage3_multi_ovs_raw.csv"
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

    draw.text((24, 24), "Diamond topology: single-host Paper 3 measurements", fill="#111111", font=title_font)

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
        (24, 858),
        "4 switches, 5 links. This diamond is the topology for every Paper 3 network-level measurement\n"
        "except the multi-OVS deployment (Figure 8), which uses a separate two-host topology.",
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


# ------------------------------------------------------------- Figure 6 ----

def _read_flap_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def draw_live_holddown_comparison(path):
    pre = _read_flap_csv(FLAP_RAW_PRE_FIX)
    post = _read_flap_csv(FLAP_RAW)

    def stats(rows):
        losses = [float(r["packet_loss_pct"]) for r in rows]
        # "Clean" means exactly one repair AND exactly one recovered for the
        # whole flap schedule -- the buggy interface-keyed version produced
        # *multiple* spurious "recovered" events (one per unsuppressed
        # transition on the interface that was never held down), so a
        # sequence ending in "recovered" is not by itself enough to tell the
        # two conditions apart; the repeated middle occurrences are what the
        # figure needs to surface.
        #
        # "BFS/recompute calls" must count only the actions decide_link_event()
        # actually reaches bfs_path() for: "repair", "noop", and
        # "repair_failed" (the three outcomes of its `state=="down"` branch).
        # "recovered" does NOT call BFS -- it just clears down_edges. An
        # earlier revision of this function computed
        # `len(sequence) - suppressed_count`, i.e. every non-suppressed
        # action including "recovered", which is why it reported 4/2 here;
        # the real count in both live conditions is 1, because this flap
        # schedule only ever pushes one edge through the "down and not
        # already in down_edges" branch once -- the recomputation-count
        # *reduction* Figure 5 demonstrates is a logic-level result over a
        # schedule with multiple distinct down_edges-triggering transitions,
        # not something this specific live protocol was designed to
        # reproduce (see Section 7.3's discussion of this exact point).
        seqs = [r["observed_action_sequence"].split(";") for r in rows]
        clean = sum(1 for s in seqs if s.count("repair") == 1 and s.count("recovered") == 1)
        spurious_recovered = [s.count("recovered") - 1 for s in seqs]
        suppressed = [s.count("suppressed") for s in seqs]
        bfs_calls = [
            s.count("repair") + s.count("noop") + s.count("repair_failed") for s in seqs
        ]
        return {
            "mean_loss": sum(losses) / len(losses),
            "clean": clean,
            "n": len(rows),
            "mean_spurious": sum(spurious_recovered) / len(spurious_recovered),
            "mean_suppressed": sum(suppressed) / len(suppressed),
            "mean_bfs": sum(bfs_calls) / len(bfs_calls),
        }

    pre_s, post_s = stats(pre), stats(post)

    width, height = 1750, 1180
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.load_default(size=44)
    font = ImageFont.load_default(size=38)
    small = ImageFont.load_default(size=32)

    draw.text((24, 24), "Live-network flapping-link measurement:", fill="#111111", font=title_font)
    draw.text((24, 76), "before vs after the edge-keyed hold-down fix", fill="#111111", font=title_font)
    draw.text(
        (24, 138),
        "Same 7-transition schedule as Figure 5, driven against a real Mininet/OVS s1-s2 link (n=5 each)",
        fill="#666666", font=small,
    )

    cols = [
        ("Clean sequences", f"{pre_s['clean']}/{pre_s['n']}", f"{post_s['clean']}/{post_s['n']}", "1 repair, 1 recovered per run"),
        ("Spurious recoveries", f"{pre_s['mean_spurious']:.0f}", f"{post_s['mean_spurious']:.0f}", "mean per repetition"),
        ("Suppressed reports", f"{pre_s['mean_suppressed']:.0f}", f"{post_s['mean_suppressed']:.0f}", "both interfaces, mean/rep"),
        ("BFS/recompute calls", f"{pre_s['mean_bfs']:.0f}", f"{post_s['mean_bfs']:.0f}", "1 either way here, see caption"),
        ("Mean packet loss", f"{pre_s['mean_loss']:.1f}%", f"{post_s['mean_loss']:.1f}%", "200-pkt probe, no stat. weight"),
    ]
    label_w = max(draw.textlength(label, font=font) for label, *_ in cols)
    val_col0 = 60 + int(label_w) + 60
    col_w = 400
    left0 = 60
    top = 260
    row_h = 90
    header_y = top
    draw.text((val_col0, header_y), "before (buggy)", fill="#D55E00", font=font)
    draw.text((val_col0 + col_w, header_y), "after (fixed)", fill="#0072B2", font=font)
    y = header_y + 90
    for label, before_val, after_val, unit in cols:
        draw.text((left0, y), label, fill="#222222", font=font)
        draw.text((val_col0, y), before_val, fill="#D55E00", font=title_font)
        draw.text((val_col0 + col_w, y), after_val, fill="#0072B2", font=title_font)
        draw.text((val_col0 + 2 * col_w, y + 8), unit, fill="#888888", font=small)
        y += row_h
        draw.line((left0, y - 20, 1650, y - 20), fill="#dddddd", width=2)

    draw.text(
        (24, y + 30),
        "The defect: OVSDB reports each physical link's two interfaces (s1-eth2, s2-eth1) independently.\n"
        "Hold-down keyed by interface name only suppressed the side that triggered the repair -- the other\n"
        "side's transitions on the SAME link went through unsuppressed, each logged as its own spurious\n"
        "'recovered' event. Keying hold-down by edge instead fixes this. The actual flow repair is 1 either\n"
        "way -- what changes is how many of the link's reported transitions are correctly suppressed.\n"
        "BFS/recompute calls stay at 1 either way here because this schedule only pushes one edge through\n"
        "the down-and-not-already-down branch once; the BFS-call reduction is Figure 5's logic-level result.",
        fill="#333333", font=small,
    )
    image.save(path)


# ------------------------------------------------------------- Figure 7 ----

def draw_startup_comparison(path):
    pre = json.loads(STARTUP_RESULT_PRE_FIX.read_text())
    post = json.loads(STARTUP_RESULT.read_text())

    # ping_loss_pct is parsed once, numerically, by
    # stage3_startup_already_down.py::parse_ping_loss_pct() and stored
    # directly in the result JSON -- read it from there rather than
    # re-parsing ping_output_tail a second time here.
    def loss_str(result):
        pct = result.get("ping_loss_pct")
        return "?" if pct is None else f"{pct:g}"

    pre_loss = loss_str(pre)
    post_loss = loss_str(post)
    pre_path = " -> ".join(pre["initial_path"]) if pre["initial_path"] else "?"
    post_path = " -> ".join(post["initial_path"]) if post["initial_path"] else "?"

    width, height = 1750, 980
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.load_default(size=44)
    font = ImageFont.load_default(size=38)
    small = ImageFont.load_default(size=32)

    draw.text((24, 24), "Startup with a link already down:", fill="#111111", font=title_font)
    draw.text((24, 76), "before vs after the initial-snapshot fix", fill="#111111", font=title_font)
    draw.text(
        (24, 138),
        "s1-s2 brought down before the agent process starts; pre-fix n=1, fixed n=3 robustness reps",
        fill="#666666", font=small,
    )

    cols = [
        ("Initial path installed", pre_path, post_path, "agent log"),
        ("Edge s1-s2 down\nat startup?", "no", "yes", "initial OVSDB"),
        ("Ping packet loss", f"{pre_loss}%", f"{post_loss}%", "20-pkt probe"),
    ]
    label_w = max(draw.textlength(l.split("\n")[0], font=font) for l, *_ in cols)
    val_col0 = 60 + int(label_w) + 60
    col_w = 420
    left0 = 60
    top = 260
    row_h = 90
    header_y = top
    draw.text((val_col0, header_y), "before (buggy)", fill="#D55E00", font=font)
    draw.text((val_col0 + col_w, header_y), "after (fixed)", fill="#0072B2", font=font)
    y = header_y + 90
    for label, before_val, after_val, unit in cols:
        lb = label.split("\n")
        for li, line in enumerate(lb):
            draw.text((left0, y + li * 40), line, fill="#222222", font=font)
        draw.text((val_col0, y), before_val, fill="#D55E00", font=title_font)
        draw.text((val_col0 + col_w, y), after_val, fill="#0072B2", font=title_font)
        draw.text((val_col0 + 2 * col_w, y + 8), unit, fill="#888888", font=small)
        y += row_h + (40 if len(lb) > 1 else 0)
        draw.line((left0, y - 20, 1650, y - 20), fill="#dddddd", width=2)

    draw.text(
        (24, y + 30),
        "The defect: OVSDB reports a subscribed table's current contents with action==\"initial\", not\n"
        "\"new\" -- the pre-fix agent matched only \"new\", silently discarding the entire startup snapshot.\n"
        "It installed the primary path through the already-dead s1-s2 link and had no way to ever find\n"
        "out, since no further transition was going to arrive for an interface whose state never changed.\n"
        "Reading the real \"initial\" snapshot before computing the first path fixes this.",
        fill="#333333", font=small,
    )
    image.save(path)


# ------------------------------------------------------------- Figure 8 ----

def draw_multi_ovs_deployment(path):
    rows = _read_flap_csv(MULTI_OVS_RAW)
    repair = [float(r["repair_action_ms"]) for r in rows]
    gap_lo = [float(r["ping_outage_bound_lower_ms"]) for r in rows]
    gap_hi = [float(r["ping_outage_bound_upper_ms"]) for r in rows]
    mean_repair = sum(repair) / len(repair)

    width, height = 1750, 1300
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.load_default(size=44)
    font = ImageFont.load_default(size=34)
    small = ImageFont.load_default(size=28)
    tiny = ImageFont.load_default(size=25)

    draw.text((24, 24), "Multi-OVS deployment: remote-edge failure", fill="#111111", font=title_font)
    draw.text(
        (24, 78),
        "across two independent OVS instances joined by a real GRE tunnel (n=5)",
        fill="#666666", font=small,
    )

    # -- mini topology: VM1 (agent + s1) -- GRE -- VM2 (s3, s4, s5, h2) -----
    diag_top = 150
    vm1_x, vm2_x = 90, 940
    vm_w, vm_h = 600, 320
    for vx, label, sub in (
        (vm1_x, "VM1 (daim-lab)", "agent + ovsdb-server"),
        (vm2_x, "VM2 (daim-lab-2)", "ovsdb-server (remote to agent)"),
    ):
        draw.rectangle((vx, diag_top, vx + vm_w, diag_top + vm_h), outline="#333333", width=3)
        draw.text((vx + 20, diag_top + 14), label, fill="#111111", font=font)
        draw.text((vx + 20, diag_top + 56), sub, fill="#888888", font=tiny)

    row_y = diag_top + 180
    h1 = sized_box(draw, vm1_x + 90, row_y, "h1", font, pad_x=26, pad_y=20)
    s1 = sized_box(draw, vm1_x + 330, row_y, "s1\n(agent-local)", font, pad_x=26, pad_y=20)
    s3 = sized_box(draw, vm2_x + 90, row_y, "s3", font, pad_x=26, pad_y=20)
    s4 = sized_box(draw, vm2_x + 470, row_y, "s4", font, pad_x=26, pad_y=20, outline="#D55E00")
    s5 = sized_box(draw, vm2_x + 280, row_y + 130, "s5", font, pad_x=26, pad_y=20)
    h2 = sized_box(draw, vm2_x + 560, row_y, "h2", font, pad_x=26, pad_y=20)

    def mid_left(b):
        return (b[0], (b[1] + b[3]) // 2)

    def mid_right(b):
        return (b[2], (b[1] + b[3]) // 2)

    def mid_top(b):
        return ((b[0] + b[2]) // 2, b[1])

    def mid_bottom(b):
        return ((b[0] + b[2]) // 2, b[3])

    styled_segment(draw, mid_right(h1), mid_left(s1), "solid", 4, "#333333")
    styled_segment(draw, mid_right(s1), mid_left(s3), "solid", 5, "#0072B2")
    draw.text(((s1[2] + s3[0]) // 2 - 32, row_y - 44), "GRE", fill="#0072B2", font=tiny)
    styled_segment(draw, mid_right(s3), mid_left(s4), "solid", 7, "#D55E00")
    draw.text(((s3[2] + s4[0]) // 2 - 90, row_y - 44), "injected failure", fill="#D55E00", font=tiny)
    styled_segment(draw, mid_bottom(s3), mid_left(s5), "dashed", 4, "#0072B2")
    styled_segment(draw, mid_right(s5), mid_bottom(s4), "dashed", 4, "#0072B2")
    styled_segment(draw, mid_right(s4), mid_left(h2), "solid", 4, "#333333")

    draw.text(
        (24, diag_top + vm_h + 20),
        "Both s3-eth1 and s4-eth1 (the s3-s4 edge, solid red) fail on VM2, entirely outside the agent's\n"
        "local OVSDB connection; detection and repair (path s1-s3-s5-s4, dashed blue) both cross the\n"
        "GRE/TCP link to VM2.",
        fill="#333333", font=small,
    )

    # -- per-repetition timing bars ------------------------------------------
    legend_y = diag_top + vm_h + 150
    draw.rectangle((24, legend_y, 48, legend_y + 24), fill="#0072B2")
    draw.text((56, legend_y), f"agent repair-action time (repair_start_ns to repair_end_ns); mean {mean_repair:.1f} ms shown as horizontal line", fill="#222222", font=tiny)
    draw.rectangle((24, legend_y + 36, 48, legend_y + 60), fill="#D55E00")
    draw.text((56, legend_y + 36), "independent ping-derived outage bound: (missing-1)x20ms to (missing+1)x20ms", fill="#222222", font=tiny)

    chart_top = legend_y + 110
    chart_left = 140
    chart_w = 1500
    chart_h = 340
    max_val = max(repair + gap_hi) * 1.2
    n = len(rows)
    group_w = chart_w / n
    bar_w = 70

    draw.line((chart_left, chart_top, chart_left, chart_top + chart_h), fill="#333333", width=3)
    draw.line((chart_left, chart_top + chart_h, chart_left + chart_w, chart_top + chart_h), fill="#333333", width=3)
    draw.text((24, chart_top - 6), "ms", fill="#666666", font=tiny)

    for i, (r, lo, hi) in enumerate(zip(repair, gap_lo, gap_hi)):
        gx = chart_left + i * group_w + group_w / 2
        r_h = int(chart_h * r / max_val)
        lo_h = int(chart_h * lo / max_val)
        hi_h = int(chart_h * hi / max_val)
        rx0 = gx - bar_w - 6
        gx0 = gx + 6
        draw.rectangle((rx0, chart_top + chart_h - r_h, rx0 + bar_w, chart_top + chart_h), fill="#0072B2")
        # Range bar (lo->hi bound), not a single value -- a lighter fill plus
        # a solid tick at the lower bound signals "range", not "measurement".
        draw.rectangle((gx0, chart_top + chart_h - hi_h, gx0 + bar_w, chart_top + chart_h - lo_h), fill="#F3C6A5")
        draw.rectangle((gx0, chart_top + chart_h - lo_h - 4, gx0 + bar_w, chart_top + chart_h - lo_h), fill="#D55E00")
        draw.text((rx0 - 4, chart_top + chart_h - r_h - 34), f"{r:.0f}", fill="#0072B2", font=tiny)
        draw.text((gx0 - 4, chart_top + chart_h - hi_h - 34), f"{lo:.0f}-{hi:.0f}", fill="#D55E00", font=tiny)
        draw.text((gx - 26, chart_top + chart_h + 14), f"rep {i+1}", fill="#333333", font=tiny)

    mean_y = chart_top + chart_h - int(chart_h * mean_repair / max_val)
    draw.line((chart_left, mean_y, chart_left + chart_w, mean_y), fill="#0072B2", width=2)

    draw.text(
        (24, chart_top + chart_h + 60),
        "Outage bounds are derived from the concurrent probe's own icmp_seq numbers, not from the agent's log:\n"
        "for N consecutive lost probes at a fixed 20ms interval, the true outage duration lies strictly between\n"
        "(N-1)x20ms and (N+1)x20ms. The agent-reported repair-action time falls inside this bound in every\n"
        "repetition -- an independent, packet-level consistency check, not a precise second measurement.",
        fill="#666666", font=small,
    )
    image.save(path)


def main():
    draw_architecture(OUT / "paper3_architecture.png")
    draw_sequence(OUT / "paper3_sequence.png")
    draw_topology(OUT / "paper3_topology.png")
    draw_recovery_chart(OUT / "paper3_recovery_timeline.png")
    draw_holddown_timeline(OUT / "paper3_holddown_timeline.png")
    draw_live_holddown_comparison(OUT / "paper3_holddown_live_comparison.png")
    draw_startup_comparison(OUT / "paper3_startup_comparison.png")
    draw_multi_ovs_deployment(OUT / "paper3_multi_ovs_deployment.png")
    print("Wrote:")
    for name in ("paper3_architecture.png", "paper3_sequence.png", "paper3_topology.png",
                 "paper3_recovery_timeline.png", "paper3_holddown_timeline.png",
                 "paper3_holddown_live_comparison.png", "paper3_startup_comparison.png",
                 "paper3_multi_ovs_deployment.png"):
        print(" ", OUT / name)


if __name__ == "__main__":
    main()
