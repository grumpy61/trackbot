#!/usr/bin/env python3
"""
yellow_ball_tracker.py

Wraps the Raspberry Pi AI Camera (IMX500) on-sensor object detector,
filters detections down to a target class ("sports ball" by default),
optionally verifies color with a quick HSV check, and tracks the best
candidate's centroid frame-to-frame with simple smoothing.

YellowBallTracker is importable (see run_trackbot.py) -- construct it, call
start(), then tick() once per frame to get a TrackResult or None.

Prints normalized steering error (dx, dy from frame center, range -1..1)
each frame when run standalone -- wire that into your motor control loop.

Based on the structure of picamera2's imx500_object_detection_demo.py
(raspberrypi/picamera2 repo).

Usage:
    python3 yellow_ball_tracker.py \
        --model ssd \
        --class-name "sports ball" \
        --color-check \
        --show-preview
"""

import argparse
import os
import sys
from dataclasses import dataclass

# Silence libcamera's benign "Unsupported V4L2 pixel format Nc30" WARN: the
# IMX500 AI Camera exposes a V4L2 node for its on-sensor NN tensor output,
# which isn't a real image format, so libcamera can't name it. Must be set
# before picamera2 (and libcamera) is imported.
# os.environ.setdefault("LIBCAMERA_LOG_LEVELS", "V4L2:ERROR")

import cv2
import numpy as np
from libcamera import Transform

from picamera2 import MappedArray, Picamera2
from picamera2.devices import IMX500
from picamera2.devices.imx500 import NetworkIntrinsics, postprocess_nanodet_detection

SMOOTHING = 0.5  # 0 = no smoothing, closer to 1 = heavier smoothing

MODEL_CONFIGS = {
    # Both .rpk files here have on-chip ("_pp" = post-processed) box decoding fused into
    # the network, so they emit already-decoded [boxes, scores, classes, count] tensors --
    # neither needs (or works with) the host-side postprocess_nanodet_detection() decode.
    "nanodet": {
        "path": "/usr/share/imx500-models/imx500_network_nanodet_plus_416x416_pp.rpk",
        "postprocess": "",
    },
    "ssd": {
        "path": "/usr/share/imx500-models/imx500_network_ssd_mobilenetv2_fpnlite_320x320_pp.rpk",
        "postprocess": "",
    },
}


@dataclass
class TrackResult:
    dx: float  # normalized steering error, -1 (left/top edge) .. +1 (right/bottom edge)
    dy: float
    size_frac: float  # box area / frame area, a rough distance proxy
    conf: float


class Detection:
    def __init__(self, coords, category, conf, metadata, imx500, picam2, frame_size):
        self.category = category
        self.conf = conf
        x, y, w, h = imx500.convert_inference_coords(coords, metadata, picam2)
        # The camera is mounted upside down, so YellowBallTracker.start() applies a
        # 180deg Transform to the ISP output (preview/color-check/overlay all see a
        # right-side-up image). But the on-sensor NN computes boxes on the raw,
        # pre-transform sensor frame, and convert_inference_coords doesn't know about
        # the transform -- so flip the box here to match the corrected image.
        frame_w, frame_h = frame_size
        self.box = (frame_w - x - w, frame_h - y - h, w, h)


class YellowBallTracker:
    """Sets up the IMX500 detector for one target class and tracks its centroid across frames."""

    def __init__(
        self,
        model="ssd",
        class_name="sports ball",
        threshold=0.12,
        iou=0.65,
        max_detections=10,
        bbox_normalization=None,
        bbox_order="yx",
        labels_path=None,
        color_check=False,
        yellow_fraction=0.08,
        color_space="hsv",
        debug=False,
        show_preview=False,
    ):
        self.class_name = class_name
        self.threshold = threshold
        self.iou = iou
        self.max_detections = max_detections
        self.color_check = color_check
        self.yellow_fraction = yellow_fraction
        self.color_space = color_space
        self.debug = debug
        self.show_preview = show_preview

        model_config = MODEL_CONFIGS[model]
        self.imx500 = IMX500(model_config["path"])
        intrinsics = self.imx500.network_intrinsics
        if not intrinsics:
            intrinsics = NetworkIntrinsics()
            intrinsics.task = "object detection"
        elif intrinsics.task != "object detection":
            raise ValueError("Network is not an object detection task")

        if labels_path is not None:
            with open(labels_path, "r") as f:
                intrinsics.labels = f.read().splitlines()
        if bbox_normalization is not None:
            intrinsics.bbox_normalization = bbox_normalization
        intrinsics.bbox_order = bbox_order
        intrinsics.postprocess = model_config["postprocess"]

        if intrinsics.labels is None:
            with open("assets/coco_labels.txt", "r") as f:
                intrinsics.labels = f.read().splitlines()
        intrinsics.update_with_defaults()
        self.intrinsics = intrinsics

        labels = intrinsics.labels
        if intrinsics.ignore_dash_labels:
            labels = [label for label in labels if label and label != "-"]
        if class_name not in labels:
            raise ValueError(f"'{class_name}' not found in model labels: {labels}")
        self.labels = labels
        self.target_label_idx = labels.index(class_name)

        self.picam2 = None
        self.frame_w = None
        self.frame_h = None
        self.tracked_center = None  # smoothed (x, y) in pixel coords
        self.last_detections = []
        self.last_box = None  # (x, y, w, h) of the last accepted detection, for draw_overlay

    def debug_print(self, msg):
        if self.debug:
            print(msg)

    def start(self):
        self.picam2 = Picamera2(self.imx500.camera_num)
        config = self.picam2.create_preview_configuration(
            controls={"FrameRate": self.intrinsics.inference_rate}, buffer_count=12,
            transform=Transform(hflip=True, vflip=True),  # camera is mounted upside down
        )
        self.imx500.show_network_fw_progress_bar()
        self.picam2.start(config, show_preview=self.show_preview)

        if self.intrinsics.preserve_aspect_ratio:
            self.imx500.set_auto_aspect_ratio()

        if self.show_preview:
            self.picam2.pre_callback = self.draw_overlay

        self.frame_w, self.frame_h = self.picam2.stream_configuration("main")["size"]

    def stop(self):
        if self.picam2 is not None:
            self.picam2.stop()

    def tick(self):
        """Capture and process one frame. Returns a TrackResult, or None if the ball wasn't found."""
        metadata = self.picam2.capture_metadata()
        detections = self._parse_detections(metadata)

        frame_rgb = None
        if self.color_check:
            frame_rgb = self.picam2.capture_array("main")

        best = self._pick_best_detection(detections, frame_rgb)
        self.last_box = best.box if best is not None else None

        if best is None:
            self.tracked_center = None
            return None

        x, y, w, h = best.box
        cx, cy = x + w / 2, y + h / 2
        if self.tracked_center is None:
            self.tracked_center = [cx, cy]
        else:
            self.tracked_center[0] = SMOOTHING * self.tracked_center[0] + (1 - SMOOTHING) * cx
            self.tracked_center[1] = SMOOTHING * self.tracked_center[1] + (1 - SMOOTHING) * cy

        dx = (self.tracked_center[0] - self.frame_w / 2) / (self.frame_w / 2)
        dy = (self.tracked_center[1] - self.frame_h / 2) / (self.frame_h / 2)
        size_frac = (w * h) / (self.frame_w * self.frame_h)
        return TrackResult(dx=dx, dy=dy, size_frac=size_frac, conf=best.conf)

    def _parse_detections(self, metadata: dict):
        """Parse the output tensor into detections, scaled to the ISP output."""
        np_outputs = self.imx500.get_outputs(metadata, add_batch=True)
        input_w, input_h = self.imx500.get_input_size()
        if np_outputs is None:
            return self.last_detections

        if self.intrinsics.postprocess == "nanodet":
            boxes, scores, classes = postprocess_nanodet_detection(
                outputs=np_outputs[0], conf=self.threshold, iou_thres=self.iou,
                max_out_dets=self.max_detections,
            )[0]
            from picamera2.devices.imx500.postprocess import scale_boxes
            boxes = scale_boxes(boxes, 1, 1, input_h, input_w, False, False)
        else:
            boxes, scores, classes = np_outputs[0][0], np_outputs[1][0], np_outputs[2][0]
            if self.intrinsics.bbox_normalization:
                boxes = boxes / input_h
            if self.intrinsics.bbox_order == "xy":
                boxes = boxes[:, [1, 0, 3, 2]]

        # DEBUG: show the raw (pre-threshold) confidence for the target class every frame,
        # so we can tell a borderline-confidence miss from a total miss.
        target_scores = [float(s) for s, c in zip(scores, classes) if int(c) == self.target_label_idx]
        best_target_score = max(target_scores, default=0.0)
        self.debug_print(
            f"DEBUG: best '{self.class_name}' score={best_target_score:.3f} (threshold={self.threshold})"
        )

        frame_size = (self.frame_w, self.frame_h)
        self.last_detections = [
            Detection(box, category, score, metadata, self.imx500, self.picam2, frame_size)
            for box, score, category in zip(boxes, scores, classes)
            if score > self.threshold
        ]
        return self.last_detections

    def _pick_best_detection(self, detections, frame_rgb=None):
        """Filter to target class, optionally verify color, return closest-to-tracked or highest-confidence box."""
        candidates = [d for d in detections if int(d.category) == self.target_label_idx]

        if self.color_check and frame_rgb is not None:
            candidates = [d for d in candidates if self._is_yellow(frame_rgb, d.box)]

        if not candidates:
            return None

        if self.tracked_center is not None:
            # Use closest-to-tracked-center as the best candidate,
            # to avoid jittery switching between multiple balls.
            def dist(d):
                x, y, w, h = d.box
                cx, cy = x + w / 2, y + h / 2
                return (cx - self.tracked_center[0]) ** 2 + (cy - self.tracked_center[1]) ** 2
            return min(candidates, key=dist)

        return max(candidates, key=lambda d: d.conf)

    def _is_yellow(self, frame_rgb, box):
        if self.color_space == "lab":
            return self._is_yellow_cielab(frame_rgb, box)
        return self._is_yellow_hsv(frame_rgb, box)

    def _is_yellow_hsv(self, frame_rgb, box):
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
        return yellow_fraction > self.yellow_fraction

    def _is_yellow_cielab(self, frame_rgb, box):
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
        if yellow_fraction > self.yellow_fraction:
            return True
        self.debug_print(
            f"DEBUG: Non yellow ball found: {yellow_fraction:.3f} less than threshold {self.yellow_fraction})"
        )
        return False

    def draw_overlay(self, request, stream="main"):
        box = self.last_box
        with MappedArray(request, stream) as m:
            if box is not None:
                x, y, w, h = box
                cv2.rectangle(m.array, (x, y), (x + w, y + h), (0, 255, 0, 0), thickness=2)
                if self.tracked_center is not None:
                    cv2.circle(
                        m.array, (int(self.tracked_center[0]), int(self.tracked_center[1])),
                        6, (255, 0, 0, 0), -1,
                    )


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model", choices=sorted(MODEL_CONFIGS), default="ssd",
        help="Which on-sensor model to use (picks the .rpk path and matching postprocess type)",
    )
    parser.add_argument("--class-name", type=str, default="sports ball",
                         help="COCO label to filter detections to")
    parser.add_argument("--threshold", type=float, default=0.12, help="Detection confidence threshold")
    parser.add_argument("--iou", type=float, default=0.65, help="IoU threshold for NMS")
    parser.add_argument("--max-detections", type=int, default=10, help="Max detections per frame")
    parser.add_argument("--bbox-normalization", action=argparse.BooleanOptionalAction, help="Normalize bbox")
    parser.add_argument("--bbox-order", choices=["yx", "xy"], default="yx",
                         help="bbox coord order: yx -> (y0,x0,y1,x1), xy -> (x0,y0,x1,y1)")
    parser.add_argument("--labels", type=str, help="Path to labels file (defaults to model's own labels)")
    parser.add_argument("--color-check", action="store_true",
                         help="Require an HSV yellow check on top of the class filter")
    parser.add_argument("--yellow-fraction", type=float, default=0.08,
                         help="Minimum fraction of box pixels that must be yellow to accept (with --color-check)")
    parser.add_argument("--color-space", choices=["hsv", "lab"], default="hsv",
                         help="Color space used for the --color-check yellow test")
    parser.add_argument("--show-preview", action="store_true", help="Show a live preview window")
    parser.add_argument("--debug", action="store_true", help="Print verbose per-frame debug info")
    return parser.parse_args()


"""" example commands:
./track_yellow_ball.py --color-check --color-space lab --show-preview

./track_yellow_ball.py --color-check --color-space lab --model nanodet --show-preview

Default uses ssd, same as above:
./track_yellow_ball.py --color-check --color-space lab --model ssd --show-preview
"""

if __name__ == "__main__":
    args = get_args()

    try:
        tracker = YellowBallTracker(
            model=args.model,
            class_name=args.class_name,
            threshold=args.threshold,
            iou=args.iou,
            max_detections=args.max_detections,
            bbox_normalization=args.bbox_normalization,
            bbox_order=args.bbox_order,
            labels_path=args.labels,
            color_check=args.color_check,
            yellow_fraction=args.yellow_fraction,
            color_space=args.color_space,
            debug=args.debug,
            show_preview=args.show_preview,
        )
    except (ValueError, RuntimeError) as e:
        print("Error initializing YellowBallTracker:", file=sys.stderr)
        print(f"  {e}", file=sys.stderr)
        sys.exit(1)

    tracker.start()

    try:
        ball_found = False
        while True:
            result = tracker.tick()
            if result is not None:
                ball_found = True
                print(
                    f"ball: dx={result.dx:+.2f} dy={result.dy:+.2f} "
                    f"size={result.size_frac:.3f} conf={result.conf:.2f}"
                )
                # TODO: feed dx, dy, size_frac into your motor control loop here
            elif ball_found:
                ball_found = False
                print("ball: not found")
    except KeyboardInterrupt:
        pass
    finally:
        tracker.stop()
