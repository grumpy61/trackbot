#!/usr/bin/env python3
"""
yellow_ball_tracker.py

Wraps the Raspberry Pi AI Camera (IMX500) on-sensor object detector,
filters detections down to a target class ("sports ball" by default),
optionally verifies color with a quick HSV check, and tracks the best
candidate's centroid frame-to-frame with simple smoothing.

Prints normalized steering error (dx, dy from frame center, range -1..1)
each frame -- wire that into your motor control loop.

Based on the structure of picamera2's imx500_object_detection_demo.py
(raspberrypi/picamera2 repo).

Usage:
    python3 yellow_ball_tracker.py \
        --model /usr/share/imx500-models/imx500_network_ssd_mobilenetv2_fpnlite_320x320_pp.rpk \
        --class-name "sports ball" \
        --color-check \
        --show-preview
"""

import argparse
import os
import sys
from functools import lru_cache

# Silence libcamera's benign "Unsupported V4L2 pixel format Nc30" WARN: the
# IMX500 AI Camera exposes a V4L2 node for its on-sensor NN tensor output,
# which isn't a real image format, so libcamera can't name it. Must be set
# before picamera2 (and libcamera) is imported.
# os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "V4L2:ERROR")

import cv2
import numpy as np

from picamera2 import MappedArray, Picamera2
from picamera2.devices import IMX500
from picamera2.devices.imx500 import NetworkIntrinsics, postprocess_nanodet_detection

last_detections = []
last_results = None
tracked_center = None  # smoothed (x, y) in pixel coords
SMOOTHING = 0.5  # 0 = no smoothing, closer to 1 = heavier smoothing


class Detection:
    def __init__(self, coords, category, conf, metadata):
        self.category = category
        self.conf = conf
        self.box = imx500.convert_inference_coords(coords, metadata, picam2)  # (x, y, w, h)


def parse_detections(metadata: dict):
    """Parse the output tensor into detections, scaled to the ISP output."""
    global last_detections
    bbox_normalization = intrinsics.bbox_normalization
    bbox_order = intrinsics.bbox_order
    threshold = args.threshold
    iou = args.iou
    max_detections = args.max_detections

    np_outputs = imx500.get_outputs(metadata, add_batch=True)
    input_w, input_h = imx500.get_input_size()
    if np_outputs is None:
        return last_detections

    if intrinsics.postprocess == "nanodet":
        boxes, scores, classes = postprocess_nanodet_detection(
            outputs=np_outputs[0], conf=threshold, iou_thres=iou, max_out_dets=max_detections
        )[0]
        from picamera2.devices.imx500.postprocess import scale_boxes
        boxes = scale_boxes(boxes, 1, 1, input_h, input_w, False, False)
    else:
        boxes, scores, classes = np_outputs[0][0], np_outputs[1][0], np_outputs[2][0]
        if bbox_normalization:
            boxes = boxes / input_h
        if bbox_order == "xy":
            boxes = boxes[:, [1, 0, 3, 2]]

    last_detections = [
        Detection(box, category, score, metadata)
        for box, score, category in zip(boxes, scores, classes)
        if score > threshold
    ]
    return last_detections


@lru_cache
def get_labels():
    labels = intrinsics.labels
    if intrinsics.ignore_dash_labels:
        labels = [label for label in labels if label and label != "-"]
    return labels


def is_yellow_hsv(frame_rgb, box):
    """Masked-pixel-fraction check: what fraction of the box falls in the yellow HSV band?"""
    x, y, w, h = [max(0, int(v)) for v in box]
    crop = frame_rgb[y:y + h, x:x + w]
    if crop.size == 0:
        return False
    hsv = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)
    # Yellow hue band in OpenCV's 0-179 scale, with sane sat/val floors
    # to reject washed-out / dark false positives.
    mask = cv2.inRange(hsv, (20, 80, 80), (35, 255, 255))
    yellow_fraction = np.count_nonzero(mask) / mask.size
    return yellow_fraction > args.yellow_fraction


def is_yellow_cielab(frame_rgb, box):
    """Masked-pixel-fraction check in CIELab: b* (blue-yellow) high and a* near-neutral-to-warm.

    CIELab separates lightness (L) from chroma (a*, b*), so this is less sensitive to
    shadows/highlights than the HSV check -- a half-shadowed ball still scores on b*.
    OpenCV packs Lab into 8-bit space with L in 0-255 (=100 * L/255) and a*/b* offset
    by 128 (0 == neutral).
    """
    x, y, w, h = [max(0, int(v)) for v in box]
    crop = frame_rgb[y:y + h, x:x + w]
    if crop.size == 0:
        return False
    lab = cv2.cvtColor(crop, cv2.COLOR_RGB2LAB)
    # b* > 128 means toward yellow; a* kept in a wide neutral-to-warm band to allow
    # for shading while still rejecting strongly green/red/blue pixels.
    mask = cv2.inRange(lab, (60, 100, 150), (255, 150, 255))
    yellow_fraction = np.count_nonzero(mask) / mask.size
    return yellow_fraction > args.yellow_fraction


def is_yellow(frame_rgb, box):
    if args.color_space == "lab":
        return is_yellow_cielab(frame_rgb, box)
    return is_yellow_hsv(frame_rgb, box)


def pick_best_detection(detections, target_label_idx, frame_rgb=None, color_check=False):
    """Filter to target class, optionally verify color, return closest-to-tracked or highest-confidence box."""
    global tracked_center
    candidates = [d for d in detections if int(d.category) == target_label_idx]

    if color_check and frame_rgb is not None:
        candidates = [d for d in candidates if is_yellow(frame_rgb, d.box)]

    if not candidates:
        return None

    if tracked_center is not None:
        def dist(d):
            x, y, w, h = d.box
            cx, cy = x + w / 2, y + h / 2
            return (cx - tracked_center[0]) ** 2 + (cy - tracked_center[1]) ** 2
        return min(candidates, key=dist)

    return max(candidates, key=lambda d: d.conf)


def draw_overlay(request, stream="main"):
    global last_results
    detections = last_results
    with MappedArray(request, stream) as m:
        if detections:
            x, y, w, h = detections.box
            cv2.rectangle(m.array, (x, y), (x + w, y + h), (0, 255, 0, 0), thickness=2)
            if tracked_center is not None:
                cv2.circle(m.array, (int(tracked_center[0]), int(tracked_center[1])), 6, (255, 0, 0, 0), -1)


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model", type=str,
        default="/usr/share/imx500-models/imx500_network_ssd_mobilenetv2_fpnlite_320x320_pp.rpk",
        help="Path to the .rpk model",
    )
    parser.add_argument("--class-name", type=str, default="sports ball",
                         help="COCO label to filter detections to")
    parser.add_argument("--threshold", type=float, default=0.5, help="Detection confidence threshold")
    parser.add_argument("--iou", type=float, default=0.65, help="IoU threshold for NMS")
    parser.add_argument("--max-detections", type=int, default=10, help="Max detections per frame")
    parser.add_argument("--bbox-normalization", action=argparse.BooleanOptionalAction, help="Normalize bbox")
    parser.add_argument("--bbox-order", choices=["yx", "xy"], default="yx",
                         help="bbox coord order: yx -> (y0,x0,y1,x1), xy -> (x0,y0,x1,y1)")
    parser.add_argument("--postprocess", choices=["", "nanodet"], default=None, help="Postprocess type")
    parser.add_argument("--labels", type=str, help="Path to labels file (defaults to model's own labels)")
    parser.add_argument("--color-check", action="store_true",
                         help="Require an HSV yellow check on top of the class filter")
    parser.add_argument("--yellow-fraction", type=float, default=0.1,
                         help="Minimum fraction of box pixels that must be yellow to accept (with --color-check)")
    parser.add_argument("--color-space", choices=["hsv", "lab"], default="hsv",
                         help="Color space used for the --color-check yellow test")
    parser.add_argument("--show-preview", action="store_true", help="Show a live preview window")
    return parser.parse_args()


if __name__ == "__main__":
    args = get_args()

    imx500 = IMX500(args.model)
    intrinsics = imx500.network_intrinsics
    if not intrinsics:
        intrinsics = NetworkIntrinsics()
        intrinsics.task = "object detection"
    elif intrinsics.task != "object detection":
        print("Network is not an object detection task", file=sys.stderr)
        sys.exit(1)

    for key, value in vars(args).items():
        if key == "labels" and value is not None:
            with open(value, "r") as f:
                intrinsics.labels = f.read().splitlines()
        elif hasattr(intrinsics, key) and value is not None:
            setattr(intrinsics, key, value)

    if intrinsics.labels is None:
        with open("assets/coco_labels.txt", "r") as f:
            intrinsics.labels = f.read().splitlines()
    intrinsics.update_with_defaults()

    labels = get_labels()
    if args.class_name not in labels:
        print(f"'{args.class_name}' not found in model labels. Available labels:\n{labels}", file=sys.stderr)
        sys.exit(1)
    target_label_idx = labels.index(args.class_name)

    picam2 = Picamera2(imx500.camera_num)
    config = picam2.create_preview_configuration(
        controls={"FrameRate": intrinsics.inference_rate}, buffer_count=12
    )
    imx500.show_network_fw_progress_bar()
    picam2.start(config, show_preview=args.show_preview)

    if intrinsics.preserve_aspect_ratio:
        imx500.set_auto_aspect_ratio()

    if args.show_preview:
        picam2.pre_callback = draw_overlay

    frame_w, frame_h = picam2.stream_configuration("main")["size"]

    try:
        ball_found = False
        while True:
            metadata = picam2.capture_metadata()
            detections = parse_detections(metadata)

            frame_rgb = None
            if args.color_check:
                frame_rgb = picam2.capture_array("main")

            best = pick_best_detection(detections, target_label_idx, frame_rgb, args.color_check)
            last_results = best

            if best is not None:
                ball_found = True
                x, y, w, h = best.box
                cx, cy = x + w / 2, y + h / 2
                if tracked_center is None:
                    tracked_center = [cx, cy]
                else:
                    tracked_center[0] = SMOOTHING * tracked_center[0] + (1 - SMOOTHING) * cx
                    tracked_center[1] = SMOOTHING * tracked_center[1] + (1 - SMOOTHING) * cy

                # Normalized steering error: -1 (left/top edge) .. +1 (right/bottom edge)
                dx = (tracked_center[0] - frame_w / 2) / (frame_w / 2)
                dy = (tracked_center[1] - frame_h / 2) / (frame_h / 2)
                size_frac = (w * h) / (frame_w * frame_h)  # rough distance proxy

                print(f"ball: dx={dx:+.2f} dy={dy:+.2f} size={size_frac:.3f} conf={best.conf:.2f}")
                # TODO: feed dx, dy, size_frac into your motor control loop here
            else:
                tracked_center = None
                if ball_found:
                    ball_found = False
                    print("ball: not found")

    except KeyboardInterrupt:
        pass
    