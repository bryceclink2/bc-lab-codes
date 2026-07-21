"""
Thermal ROI Video Analyzer

Run:
    pip install opencv-python numpy matplotlib pandas pillow pytesseract
    python thermal_roi_analyzer.py

    Also install the Tesseract OCR application:
    https://github.com/UB-Mannheim/tesseract/wiki

What it does:
    - Opens a video file.
    - Lets you draw a circular region of interest (ROI) on the video.
    - Samples the ROI every 0.5 seconds.
    - Converts ROI pixel colors to temperature using the selected color bar.
    - Reports:
        1. average temperature inside the circular ROI
        2. average temperature after excluding the hottest region
    - Shows the video and a live graph while it runs.
    - Can recalibrate every 0.5 seconds by reading high/low text from selected
      boxes above/below the video's color bar, pausing for manual Temp Min/Max
      entry, or reading nearest matching values from a calibration CSV.
    - Saves results to CSV.

Important calibration note:
    This script assumes the video is a normal color/gray thermal export, not a
    radiometric file. If your thermal camera exports true radiometric data, use
    the manufacturer's SDK for exact temperatures.

    For normal thermal videos, temperature is estimated by matching ROI colors
    to the selected color bar. Draw the color bar box around the colored contour
    only, with the high color at the top and the low color at the bottom.
"""

from __future__ import annotations

import csv
import math
import os
import re
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk

import cv2
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from PIL import Image, ImageTk


SAMPLE_INTERVAL_SECONDS = 0.5
MAX_EXPECTED_TEMPERATURE = 150.0


@dataclass
class CircularROI:
    cx: int
    cy: int
    radius: int


@dataclass
class RectBox:
    x1: int
    y1: int
    x2: int
    y2: int

    def normalized(self) -> "RectBox":
        return RectBox(
            min(self.x1, self.x2),
            min(self.y1, self.y2),
            max(self.x1, self.x2),
            max(self.y1, self.y2),
        )


@dataclass
class CalibrationRead:
    temp_max: float
    temp_min: float
    source: str
    low_text: str = ""
    high_text: str = ""


class ThermalROIAnalyzer(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Thermal ROI Video Analyzer")
        self.geometry("3740x2500")
        self.minsize(980, 640)

        self.video_path: str | None = None
        self.cap: cv2.VideoCapture | None = None
        self.fps = 30.0
        self.frame_count = 0
        self.duration_seconds = 0.0

        self.current_frame: np.ndarray | None = None
        self.current_frame_rgb: np.ndarray | None = None
        self.display_scale = 1.0
        self.roi: CircularROI | None = None
        self.color_bar_box: RectBox | None = None
        self.high_temp_box: RectBox | None = None
        self.low_temp_box: RectBox | None = None
        self.selection_mode = tk.StringVar(value="roi")
        self.drag_start: tuple[int, int] | None = None

        self.is_running = False
        self.worker: threading.Thread | None = None
        self.results: list[dict[str, float]] = []
        self.calibration_df: pd.DataFrame | None = None

        self._build_ui()

    def _build_ui(self) -> None:
        main = ttk.Frame(self, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        controls = ttk.Frame(main)
        controls.pack(fill=tk.X)

        ttk.Button(controls, text="Open Video", command=self.open_video).pack(side=tk.LEFT)
        ttk.Button(controls, text="Load Calibration CSV", command=self.load_calibration_csv).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(controls, text="Draw ROI", command=lambda: self.set_selection_mode("roi")).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(controls, text="Draw Color Bar", command=lambda: self.set_selection_mode("bar")).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(controls, text="Draw High Box", command=lambda: self.set_selection_mode("high")).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(controls, text="Draw Low Box", command=lambda: self.set_selection_mode("low")).pack(
            side=tk.LEFT, padx=(8, 0)
        )
        ttk.Button(controls, text="Start", command=self.start_analysis).pack(side=tk.LEFT, padx=(16, 0))
        ttk.Button(controls, text="Stop", command=self.stop_analysis).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(controls, text="Save CSV", command=self.save_csv).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(controls, text="Temp Min").pack(side=tk.LEFT, padx=(24, 4))
        self.temp_min_var = tk.DoubleVar(value=20.0)
        ttk.Entry(controls, textvariable=self.temp_min_var, width=8).pack(side=tk.LEFT)

        ttk.Label(controls, text="Temp Max").pack(side=tk.LEFT, padx=(10, 4))
        self.temp_max_var = tk.DoubleVar(value=80.0)
        ttk.Entry(controls, textvariable=self.temp_max_var, width=8).pack(side=tk.LEFT)

        ttk.Label(controls, text="Exclude hottest %").pack(side=tk.LEFT, padx=(16, 4))
        self.hot_percent_var = tk.DoubleVar(value=2.0)
        ttk.Entry(controls, textvariable=self.hot_percent_var, width=6).pack(side=tk.LEFT)

        self.pause_calibration_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            controls,
            text="Pause for calibration every sample",
            variable=self.pause_calibration_var,
        ).pack(side=tk.LEFT, padx=(16, 0))

        self.status_var = tk.StringVar(
            value="Open a video, draw the circular ROI, then draw boxes around the high and low color-bar numbers."
        )
        ttk.Label(main, textvariable=self.status_var).pack(fill=tk.X, pady=(8, 6))

        body = ttk.PanedWindow(main, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)

        video_panel = ttk.Frame(body)
        body.add(video_panel, weight=3)

        self.video_canvas = tk.Canvas(video_panel, bg="black", highlightthickness=0)
        self.video_canvas.pack(fill=tk.BOTH, expand=True)
        self.video_canvas.bind("<ButtonPress-1>", self.on_mouse_down)
        self.video_canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.video_canvas.bind("<ButtonRelease-1>", self.on_mouse_up)

        graph_panel = ttk.Frame(body)
        body.add(graph_panel, weight=2)

        self.figure, (self.ax, self.cal_ax) = plt.subplots(2, 1, figsize=(5, 5), dpi=100, sharex=True)
        self.ax.set_title("ROI Temperature Over Time")
        self.ax.set_ylabel("Temperature")
        self.ax.grid(True, alpha=0.3)
        self.full_line, = self.ax.plot([], [], label="Full ROI average")
        self.excl_line, = self.ax.plot([], [], label="Excluding hottest region")
        self.ax.legend(loc="best")

        self.cal_ax.set_title("Read Calibration Values")
        self.cal_ax.set_xlabel("Time (s)")
        self.cal_ax.set_ylabel("Temperature")
        self.cal_ax.grid(True, alpha=0.3)
        self.low_line, = self.cal_ax.plot([], [], label="Read low", color="tab:cyan")
        self.high_line, = self.cal_ax.plot([], [], label="Read high", color="tab:red")
        self.cal_ax.legend(loc="best")
        self.figure.tight_layout()

        self.graph_canvas = FigureCanvasTkAgg(self.figure, master=graph_panel)
        self.graph_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        self.table = ttk.Treeview(
            graph_panel,
            columns=("time", "full_avg", "excl_avg", "low", "high", "source"),
            show="headings",
            height=8,
        )
        self.table.heading("time", text="Time (s)")
        self.table.heading("full_avg", text="Full Avg")
        self.table.heading("excl_avg", text="Avg Excl. Hot Region")
        self.table.heading("low", text="Low")
        self.table.heading("high", text="High")
        self.table.heading("source", text="Source")
        self.table.column("time", width=90, anchor=tk.CENTER)
        self.table.column("full_avg", width=120, anchor=tk.CENTER)
        self.table.column("excl_avg", width=160, anchor=tk.CENTER)
        self.table.column("low", width=80, anchor=tk.CENTER)
        self.table.column("high", width=80, anchor=tk.CENTER)
        self.table.column("source", width=100, anchor=tk.CENTER)
        self.table.pack(fill=tk.X, pady=(8, 0))

    def set_selection_mode(self, mode: str) -> None:
        self.selection_mode.set(mode)
        labels = {
            "roi": "Drag from the center outward to draw the circular measurement ROI.",
            "bar": "Drag a rectangle around the colored contour only, high color at top and low color at bottom.",
            "high": "Drag a rectangle around the color-bar high temperature number.",
            "low": "Drag a rectangle around the color-bar low temperature number.",
        }
        self.status_var.set(labels[mode])

    def open_video(self) -> None:
        path = filedialog.askopenfilename(
            title="Select thermal video",
            filetypes=[
                ("Video files", "*.mp4 *.avi *.mov *.mkv *.wmv *.m4v"),
                ("All files", "*.*"),
            ],
        )
        if not path:
            return

        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            messagebox.showerror("Video error", "Could not open that video file.")
            return

        self.video_path = path
        self.cap = cap
        self.fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.duration_seconds = self.frame_count / self.fps if self.fps else 0.0
        self.results.clear()
        self.roi = None
        self.color_bar_box = None
        self.high_temp_box = None
        self.low_temp_box = None
        self.clear_table()
        self.update_graph()

        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ok, frame = cap.read()
        if ok:
            self.show_frame(frame)
            self.status_var.set(
                f"Loaded {os.path.basename(path)}. Draw ROI, Color Bar, High Box, and Low Box before starting."
            )

    def load_calibration_csv(self) -> None:
        path = filedialog.askopenfilename(
            title="Select calibration CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return

        df = pd.read_csv(path)
        required = {"time_s", "temp_min", "temp_max"}
        if not required.issubset(set(df.columns)):
            messagebox.showerror(
                "Calibration CSV error",
                "CSV must contain columns: time_s, temp_min, temp_max",
            )
            return

        self.calibration_df = df.sort_values("time_s").reset_index(drop=True)
        self.status_var.set(f"Loaded calibration CSV: {os.path.basename(path)}")

    def on_mouse_down(self, event: tk.Event) -> None:
        self.drag_start = (event.x, event.y)

    def on_mouse_drag(self, event: tk.Event) -> None:
        if self.drag_start is None:
            return
        if self.selection_mode.get() == "roi":
            self.draw_roi_preview(event.x, event.y)
        else:
            self.draw_box_preview(event.x, event.y)

    def on_mouse_up(self, event: tk.Event) -> None:
        if self.drag_start is None or self.current_frame is None:
            return

        mode = self.selection_mode.get()
        if mode == "roi":
            x0, y0 = self.drag_start
            x1, y1 = event.x, event.y
            radius_display = int(math.hypot(x1 - x0, y1 - y0))
            if radius_display < 4:
                self.status_var.set("ROI is too small. Drag a larger circle.")
                return

            cx = int(x0 / self.display_scale)
            cy = int(y0 / self.display_scale)
            radius = int(radius_display / self.display_scale)
            h, w = self.current_frame.shape[:2]
            cx = max(0, min(w - 1, cx))
            cy = max(0, min(h - 1, cy))
            radius = max(1, min(radius, w, h))
            self.roi = CircularROI(cx, cy, radius)
            message = f"ROI selected: center=({cx}, {cy}), radius={radius}px."
        else:
            box = self.event_rect_to_frame_rect(self.drag_start[0], self.drag_start[1], event.x, event.y)
            if box is None:
                self.status_var.set("Box is too small. Drag a larger rectangle around the number.")
                return
            if mode == "bar":
                self.color_bar_box = box
                message = f"Color bar box selected: {box}."
            elif mode == "high":
                self.high_temp_box = box
                message = f"High temperature OCR box selected: {box}."
            else:
                self.low_temp_box = box
                message = f"Low temperature OCR box selected: {box}."

        self.drag_start = None
        self.show_frame(self.current_frame)
        self.status_var.set(message)

    def draw_roi_preview(self, x: int, y: int) -> None:
        self.video_canvas.delete("roi")
        if self.drag_start is None:
            return
        x0, y0 = self.drag_start
        r = math.hypot(x - x0, y - y0)
        self.video_canvas.create_oval(x0 - r, y0 - r, x0 + r, y0 + r, outline="yellow", width=2, tags="roi")

    def draw_box_preview(self, x: int, y: int) -> None:
        self.video_canvas.delete("preview_box")
        if self.drag_start is None:
            return
        x0, y0 = self.drag_start
        mode = self.selection_mode.get()
        color = "lime" if mode == "bar" else "red" if mode == "high" else "cyan"
        self.video_canvas.create_rectangle(x0, y0, x, y, outline=color, width=2, tags="preview_box")

    def event_rect_to_frame_rect(self, x0: int, y0: int, x1: int, y1: int) -> RectBox | None:
        if self.current_frame is None:
            return None
        h, w = self.current_frame.shape[:2]
        box = RectBox(
            int(x0 / self.display_scale),
            int(y0 / self.display_scale),
            int(x1 / self.display_scale),
            int(y1 / self.display_scale),
        ).normalized()
        box.x1 = max(0, min(w - 1, box.x1))
        box.x2 = max(0, min(w, box.x2))
        box.y1 = max(0, min(h - 1, box.y1))
        box.y2 = max(0, min(h, box.y2))
        if box.x2 - box.x1 < 4 or box.y2 - box.y1 < 4:
            return None
        return box

    def show_frame(self, frame_bgr: np.ndarray) -> None:
        self.current_frame = frame_bgr.copy()
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        self.current_frame_rgb = rgb

        canvas_w = max(1, self.video_canvas.winfo_width())
        canvas_h = max(1, self.video_canvas.winfo_height())
        h, w = rgb.shape[:2]
        self.display_scale = min(canvas_w / w, canvas_h / h)
        new_w = max(1, int(w * self.display_scale))
        new_h = max(1, int(h * self.display_scale))

        resized = cv2.resize(rgb, (new_w, new_h), interpolation=cv2.INTER_AREA)
        image = Image.fromarray(resized)
        self.tk_image = ImageTk.PhotoImage(image)

        self.video_canvas.delete("all")
        self.video_canvas.create_image(0, 0, anchor=tk.NW, image=self.tk_image)

        if self.roi:
            cx = self.roi.cx * self.display_scale
            cy = self.roi.cy * self.display_scale
            r = self.roi.radius * self.display_scale
            self.video_canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline="yellow", width=2, tags="roi")

        self.draw_saved_box(self.color_bar_box, "lime", "BAR")
        self.draw_saved_box(self.high_temp_box, "red", "HIGH")
        self.draw_saved_box(self.low_temp_box, "cyan", "LOW")

    def draw_saved_box(self, box: RectBox | None, color: str, label: str) -> None:
        if box is None:
            return
        x1 = box.x1 * self.display_scale
        y1 = box.y1 * self.display_scale
        x2 = box.x2 * self.display_scale
        y2 = box.y2 * self.display_scale
        self.video_canvas.create_rectangle(x1, y1, x2, y2, outline=color, width=2)
        self.video_canvas.create_text(x1 + 4, y1 + 4, anchor=tk.NW, text=label, fill=color)

    def start_analysis(self) -> None:
        if self.cap is None or self.video_path is None:
            messagebox.showinfo("Open video", "Open a video first.")
            return
        if self.roi is None:
            messagebox.showinfo("Select ROI", "Drag a circular ROI on the video first.")
            return
        if self.color_bar_box is None:
            messagebox.showinfo(
                "Select color bar",
                "Draw a box around the colored temperature bar. Without it, the app cannot map video colors to temperatures.",
            )
            return
        if self.is_running:
            return

        self.is_running = True
        self.results.clear()
        self.clear_table()
        self.update_graph()
        self.worker = threading.Thread(target=self.analysis_loop, daemon=True)
        self.worker.start()

    def stop_analysis(self) -> None:
        self.is_running = False
        self.status_var.set("Stopped.")

    def analysis_loop(self) -> None:
        assert self.cap is not None
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        sample_index = 0

        while self.is_running:
            t_s = sample_index * SAMPLE_INTERVAL_SECONDS
            if self.duration_seconds and t_s > self.duration_seconds:
                break

            frame_index = int(round(t_s * self.fps))
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = self.cap.read()
            if not ok:
                break

            self.after(0, self.show_frame, frame)

            calibration = self.get_calibration_for_frame(frame, t_s)

            if self.pause_calibration_var.get():
                self.after(0, self.status_var.set, f"Paused at {t_s:.1f}s. Confirm or edit OCR high/low values.")
                self.after(0, self.temp_max_var.set, calibration.temp_max)
                self.after(0, self.temp_min_var.set, calibration.temp_min)
                pause_event = threading.Event()
                self.after(0, lambda: self.ask_calibration_continue(t_s, pause_event))
                pause_event.wait()
                if not self.is_running:
                    break
                calibration = CalibrationRead(
                    temp_min=float(self.temp_max_var.get()),
                    temp_max=float(self.temp_min_var.get()),
                    source=f"manual_popup_after_{calibration.source}",
                    high_text=calibration.high_text,
                    low_text=calibration.low_text,
                )

            full_avg, excl_avg, hottest_count = self.measure_frame(frame, calibration.temp_max, calibration.temp_min)
            row = {
                "time_s": round(t_s, 3),
                "roi_avg_temp": round(full_avg, 4),
                "roi_avg_excluding_hottest_region": round(excl_avg, 4),
                "temp_max": float(calibration.temp_max),
                "temp_min": float(calibration.temp_min),
                "calibration_source": calibration.source,
                "ocr_high_text": calibration.high_text,
                "ocr_low_text": calibration.low_text,
                "excluded_pixel_count": int(hottest_count),
            }
            self.results.append(row)
            self.after(0, self.add_result_to_ui, row)

            sample_index += 1
            time.sleep(0.03)

        self.is_running = False
        self.after(0, self.status_var.set, "Analysis complete. Save CSV when ready.")

    def ask_calibration_continue(self, t_s: float, event: threading.Event) -> None:
        dialog = tk.Toplevel(self)
        dialog.title(f"Calibration at {t_s:.1f}s")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)
        dialog.update_idletasks()

        width = 700
        height = 350

        screen_width = dialog.winfo_screenwidth()
        screen_height = dialog.winfo_screenheight()

        x = (screen_width // 2) - (width // 2)
        y = (screen_height // 2) - (height // 2)

        dialog.geometry(f"{width}x{height}+{x}+{y}")

        entry_font = ("Segoe UI", 20)
        label_font = ("Segoe UI", 20)

        style = ttk.Style()
        style.configure("Big.TButton", font=("Segoe UI", 18))

        content = ttk.Frame(dialog, padding=30)
        content.pack(fill=tk.BOTH, expand=True)
        ttk.Label(content, text=f"Frame time: {t_s:.1f}s", font=label_font).grid(row=0, column=0, columnspan=2, sticky=tk.W)
        ttk.Label(content, text="High temperature", font=label_font).grid(row=1, column=0, sticky=tk.W, pady=(8, 0))
        ttk.Label(content, text="Low temperature", font=label_font).grid(row=2, column=0, sticky=tk.W, pady=(10, 0))

        high_var = tk.DoubleVar(value=float(self.temp_max_var.get()))
        low_var = tk.DoubleVar(value=float(self.temp_min_var.get()))
        high_entry = ttk.Entry(content, textvariable=high_var, width=15, font=entry_font)
        low_entry = ttk.Entry(content, textvariable=low_var, width=15, font=entry_font)
        high_entry.grid(row=1, column=1, sticky=tk.EW, padx=(10, 0), pady=(10, 0))
        low_entry.grid(row=2, column=1, sticky=tk.EW, padx=(10, 0), pady=(8, 0))

        def apply_and_close() -> None:
            try:
                high = float(high_var.get())
                low = float(low_var.get())
            except tk.TclError:
                messagebox.showerror("Invalid value", "Enter numeric low and high temperatures.", parent=dialog)
                return
            if high <= low:
                messagebox.showerror("Invalid range", "High temperature must be greater than low temperature.", parent=dialog)
                return
            self.temp_max_var.set(high)
            self.temp_min_var.set(low)
            dialog.destroy()
            event.set()

        def stop_from_popup() -> None:
            self.is_running = False
            dialog.destroy()
            event.set()
            self.status_var.set("Stopped from calibration popup. Save CSV when ready.")

        ttk.Button(content, text="Use These Values", style="Big.TButton", command=apply_and_close).grid(
            row=3, column=0, sticky=tk.EW, pady=(14, 0)
        )
        ttk.Button(content, text="Stop Analysis", style="Big.TButton", command=stop_from_popup).grid(
            row=3, column=1, sticky=tk.EW, padx=(10, 0), pady=(14, 0)
        )
        dialog.protocol("WM_DELETE_WINDOW", apply_and_close)
        low_entry.focus_set()

    def get_calibration_for_frame(self, frame_bgr: np.ndarray, t_s: float) -> CalibrationRead:
        if self.high_temp_box is not None and self.low_temp_box is not None:
            try:
                temp_max, high_text = self.ocr_temperature(frame_bgr, self.high_temp_box)
                temp_min, low_text = self.ocr_temperature(frame_bgr, self.low_temp_box)
                if temp_max <= temp_min:
                    raise RuntimeError(f"OCR read invalid range: low={temp_min}, high={temp_max}")
                self.after(0, self.temp_max_var.set, temp_max)
                self.after(0, self.temp_min_var.set, temp_min)
                return CalibrationRead(temp_max, temp_min, "ocr_boxes", high_text, low_text)
            except Exception as exc:
                previous = self.latest_successful_ocr_calibration()
                if previous is not None:
                    self.after(
                        0,
                        self.status_var.set,
                        f"OCR failed at {t_s:.1f}s; using previous OCR values. {exc}",
                    )
                    return CalibrationRead(
                        previous.temp_max,
                        previous.temp_min,
                        "previous_ocr_after_ocr_fail",
                        previous.high_text,
                        previous.low_text,
                    )
                self.after(
                    0,
                    self.status_var.set,
                    f"OCR calibration failed at {t_s:.1f}s; no previous OCR read exists. {exc}",
                )

        if self.calibration_df is None or self.calibration_df.empty:
            source = "manual_fields_after_ocr_fail" if self.high_temp_box and self.low_temp_box else "manual_fields"
            return CalibrationRead(float(self.temp_max_var.get()), float(self.temp_min_var.get()), source)

        idx = (self.calibration_df["time_s"] - t_s).abs().idxmin()
        row = self.calibration_df.loc[idx]
        source = "calibration_csv_after_ocr_fail" if self.high_temp_box and self.low_temp_box else "calibration_csv"
        return CalibrationRead(float(row["temp_max"]), float(row["temp_min"]), source)

    def latest_successful_ocr_calibration(self) -> CalibrationRead | None:
        for row in reversed(self.results):
            if row.get("calibration_source") == "ocr_boxes":
                return CalibrationRead(
                    float(row["temp_max"]),
                    float(row["temp_min"]),
                    "ocr_boxes",
                    str(row.get("ocr_high_text", "")),
                    str(row.get("ocr_low_text", "")),
                )
        return None

    def ocr_temperature(self, frame_bgr: np.ndarray, box: RectBox) -> tuple[float, str]:
        try:
            import pytesseract
        except ImportError as exc:
            raise RuntimeError("Install pytesseract and the Tesseract OCR app.") from exc

        crop = frame_bgr[box.y1 : box.y2, box.x1 : box.x2]
        if crop.size == 0:
            raise RuntimeError("OCR box has no pixels.")

        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, None, fx=6, fy=6, interpolation=cv2.INTER_CUBIC)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        _, bright_digits = cv2.threshold(gray, 170, 255, cv2.THRESH_BINARY)
        adaptive = cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            31,
            -6,
        )

        kernel = np.ones((2, 2), np.uint8)
        bright_digits = cv2.morphologyEx(bright_digits, cv2.MORPH_CLOSE, kernel)

        variants = []
        for image in (bright_digits, otsu, adaptive):
            variants.append(image)
            variants.append(cv2.bitwise_not(image))

        padded_variants = [
            cv2.copyMakeBorder(image, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=255)
            for image in variants
        ]

        configs = [
            "--oem 3 --psm 7 -c tessedit_char_whitelist=-0123456789.",
            "--oem 3 --psm 8 -c tessedit_char_whitelist=-0123456789.",
            "--oem 3 --psm 13 -c tessedit_char_whitelist=-0123456789.",
            "--oem 3 --psm 6 -c tessedit_char_whitelist=-0123456789.",
        ]
        texts = []
        for image in padded_variants:
            texts.extend(pytesseract.image_to_string(image, config=config) for config in configs)
        text = " ".join(texts)
        text = text.replace("O", "0").replace("o", "0")
        value = self.parse_fixed_decimal_temperature(text)
        if value is None:
            raise RuntimeError(f"Could not read a number from OCR text: {text!r}")
        return value, text.strip()

    def parse_fixed_decimal_temperature(self, text: str) -> float | None:
        cleaned = (
            text.replace("O", "0")
            .replace("o", "0")
            .replace(",", ".")
            .replace(" ", "")
            .replace("\n", "")
        )

        fixed_matches = re.findall(r"-?\d{1,3}\.\d", cleaned)
        for match in fixed_matches:
            accepted = self.accept_temperature_if_reasonable(float(match))
            if accepted is not None:
                return accepted

        numeric_matches = re.findall(r"-?\d+(?:\.\d+)?", cleaned)
        if not numeric_matches:
            return None

        for token in numeric_matches:
            if "." in token:
                accepted = self.accept_temperature_if_reasonable(float(token))
                if accepted is not None:
                    return accepted
                continue

            sign = -1.0 if token.startswith("-") else 1.0
            digits = token.lstrip("-")
            if len(digits) >= 3:
                accepted = self.accept_temperature_if_reasonable(sign * float(f"{digits[:-1]}.{digits[-1]}"))
            else:
                accepted = self.accept_temperature_if_reasonable(sign * float(digits))
            if accepted is not None:
                return accepted
        return None

    def accept_temperature_if_reasonable(self, value: float) -> float | None:
        if value > MAX_EXPECTED_TEMPERATURE:
            return None
        return value

    def measure_frame(self, frame_bgr: np.ndarray, temp_min: float, temp_max: float) -> tuple[float, float, int]:
        if self.roi is None:
            raise RuntimeError("ROI has not been selected.")
        if self.color_bar_box is None:
            raise RuntimeError("Color bar box has not been selected.")

        temp = self.frame_to_temperature_from_colorbar(frame_bgr, temp_min, temp_max)

        h, w = temp.shape
        yy, xx = np.ogrid[:h, :w]
        mask = (xx - self.roi.cx) ** 2 + (yy - self.roi.cy) ** 2 <= self.roi.radius ** 2
        roi_values = temp[mask]
        full_avg = float(np.mean(roi_values))

        hot_percent = max(0.0, min(100.0, float(self.hot_percent_var.get())))
        if hot_percent <= 0 or roi_values.size < 2:
            return full_avg, full_avg, 0

        threshold = np.percentile(roi_values, 100.0 - hot_percent)
        hot_seed_mask = mask & (temp >= threshold)

        # Keep only the connected hot region containing the hottest pixel.
        binary = hot_seed_mask.astype(np.uint8)
        num_labels, labels = cv2.connectedComponents(binary)
        hottest_y, hottest_x = np.unravel_index(np.argmax(np.where(mask, temp, -np.inf)), temp.shape)
        hottest_label = labels[hottest_y, hottest_x]

        if num_labels <= 1 or hottest_label == 0:
            excluded_mask = hot_seed_mask
        else:
            excluded_mask = labels == hottest_label

        keep_mask = mask & ~excluded_mask
        kept_values = temp[keep_mask]
        if kept_values.size == 0:
            excluded_avg = full_avg
        else:
            excluded_avg = float(np.mean(kept_values))

        return full_avg, excluded_avg, int(np.count_nonzero(excluded_mask))

    def frame_to_temperature_from_colorbar(
        self,
        frame_bgr: np.ndarray,
        temp_min: float,
        temp_max: float,
    ) -> np.ndarray:
        assert self.color_bar_box is not None
        box = self.color_bar_box.normalized()
        bar = frame_bgr[box.y1 : box.y2, box.x1 : box.x2]
        if bar.size == 0:
            raise RuntimeError("Color bar box has no pixels.")

        frame_lab = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
        bar_lab = cv2.cvtColor(bar, cv2.COLOR_BGR2LAB).astype(np.float32)

        if bar.shape[0] >= bar.shape[1]:
            color_profile = np.median(bar_lab, axis=1)
            profile_temps = np.linspace(temp_max, temp_min, len(color_profile), dtype=np.float32)
        else:
            color_profile = np.median(bar_lab, axis=0)
            profile_temps = np.linspace(temp_min, temp_max, len(color_profile), dtype=np.float32)

        if len(color_profile) < 2:
            raise RuntimeError("Color bar box is too small.")

        pixels = frame_lab.reshape(-1, 3)
        out = np.empty((pixels.shape[0],), dtype=np.float32)
        chunk_size = 50000
        weights = np.array([0.35, 1.0, 1.0], dtype=np.float32)
        for start in range(0, pixels.shape[0], chunk_size):
            chunk = pixels[start : start + chunk_size]
            diff = (chunk[:, None, :] - color_profile[None, :, :]) * weights
            distances = np.sum(diff**2, axis=2)
            nearest = np.argmin(distances, axis=1)
            out[start : start + chunk_size] = profile_temps[nearest]

        return out.reshape(frame_bgr.shape[:2])

    def add_result_to_ui(self, row: dict[str, float]) -> None:
        self.table.insert(
            "",
            tk.END,
            values=(
                f"{row['time_s']:.1f}",
                f"{row['roi_avg_temp']:.3f}",
                f"{row['roi_avg_excluding_hottest_region']:.3f}",
                f"{row['temp_min']:.3f}",
                f"{row['temp_max']:.3f}",
                row["calibration_source"],
            ),
        )
        self.table.yview_moveto(1)
        self.update_graph()
        self.status_var.set(
            f"{row['time_s']:.1f}s: avg={row['roi_avg_temp']:.3f}, "
            f"excluding hot region={row['roi_avg_excluding_hottest_region']:.3f}, "
            f"low={row['temp_min']:.3f}, high={row['temp_max']:.3f}, source={row['calibration_source']}"
        )

    def update_graph(self) -> None:
        times = [row["time_s"] for row in self.results]
        full = [row["roi_avg_temp"] for row in self.results]
        excl = [row["roi_avg_excluding_hottest_region"] for row in self.results]
        lows = [row["temp_min"] for row in self.results]
        highs = [row["temp_max"] for row in self.results]

        self.full_line.set_data(times, full)
        self.excl_line.set_data(times, excl)
        self.low_line.set_data(times, highs)
        self.high_line.set_data(times, lows)
        self.ax.relim()
        self.ax.autoscale_view()
        self.cal_ax.relim()
        self.cal_ax.autoscale_view()
        self.graph_canvas.draw_idle()

    def clear_table(self) -> None:
        for item in self.table.get_children():
            self.table.delete(item)

    def save_csv(self) -> None:
        if not self.results:
            messagebox.showinfo("No results", "Run the analysis before saving.")
            return

        default_name = "thermal_roi_results.csv"
        if self.video_path:
            base = os.path.splitext(os.path.basename(self.video_path))[0]
            default_name = f"{base}_thermal_roi_results.csv"

        path = filedialog.asksaveasfilename(
            title="Save results CSV",
            defaultextension=".csv",
            initialfile=default_name,
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(self.results[0].keys()))
            writer.writeheader()
            writer.writerows(self.results)

        self.status_var.set(f"Saved results to {path}")


if __name__ == "__main__":
    try:
        app = ThermalROIAnalyzer()
        app.mainloop()
    except Exception as exc:
        messagebox.showerror("Error", str(exc))
