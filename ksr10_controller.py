#!/usr/bin/env python3
"""
KSR10 / OWI-535 USB controller with hold-to-run buttons
and 5 recordable movement presets.
LED polarity is inverted to match the hardware.
"""

import tkinter as tk
from tkinter import messagebox
import usb.core
import usb.util
import threading
import time

VENDOR_ID = 0x1267
PRODUCT_IDS = [0x0000, 0x0001]

BITS = {
    "gripper_close": 16,
    "gripper_open":  17,
    "wrist_up":      18,
    "wrist_down":    19,
    "elbow_up":      20,
    "elbow_down":    21,
    "shoulder_up":   22,
    "shoulder_down": 23,
    "base_right":     8,
    "base_left":      9,
    "led":            0,
}

class KSR10Controller:
    def __init__(self):
        self.dev = None
        self.state = 0
        self.lock = threading.Lock()
        self.connect()

    def connect(self):
        for pid in PRODUCT_IDS:
            self.dev = usb.core.find(idVendor=VENDOR_ID, idProduct=pid)
            if self.dev is not None:
                break
        if self.dev is None:
            raise RuntimeError(
                "KSR10 / OWI-535 not found (VID 1267, PID 0000/0001).\n"
                "Make sure the arm is plugged in and powered, and that a "
                "libusb/WinUSB driver is installed (Zadig)."
            )
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except (usb.core.USBError, NotImplementedError):
            pass
        self.dev.set_configuration()
        self.stop_all()

    def set_bit(self, bit, on):
        with self.lock:
            if on:
                self.state |= (1 << bit)
            else:
                self.state &= ~(1 << bit)
            self._write()

    def _write(self):
        data = [
            (self.state >> 16) & 0xFF,
            (self.state >> 8) & 0xFF,
            self.state & 0xFF,
        ]
        try:
            self.dev.ctrl_transfer(0x40, 0x06, 0x0100, 0x00, data)
        except usb.core.USBError as e:
            print(f"USB write error: {e}")

    def stop_all(self):
        with self.lock:
            self.state = 0
            self._write()

    def close(self):
        self.stop_all()
        if self.dev:
            usb.util.dispose_resources(self.dev)


class App:
    def __init__(self, arm):
        self.arm = arm
        self.root = tk.Tk()
        self.root.title("KSR10 / OWI-535 Controller + Presets")
        self.root.resizable(False, False)

        self.presets = [[] for _ in range(5)]
        self.recording_slot = None
        self.hold_starts = {}
        self.playback_stop = threading.Event()
        self.playback_thread = None
        self.led_on = False

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self):
        main = tk.Frame(self.root, padx=10, pady=10)
        main.pack()

        motors = tk.LabelFrame(main, text="Motors (hold to run)", padx=8, pady=8)
        motors.grid(row=0, column=0, sticky="n", padx=(0, 10))

        def make_btn(parent, text, bit, r, c):
            btn = tk.Button(parent, text=text, width=12, height=2)
            btn.bind("<ButtonPress-1>", lambda e, b=bit: self.on_press(b))
            btn.bind("<ButtonRelease-1>", lambda e, b=bit: self.on_release(b))
            btn.grid(row=r, column=c, padx=4, pady=3)
            return btn

        tk.Label(motors, text="M1 Gripper",  font=("Segoe UI", 9, "bold")).grid(row=0, column=0, columnspan=2)
        make_btn(motors, "Open",  BITS["gripper_open"],  1, 0)
        make_btn(motors, "Close", BITS["gripper_close"], 1, 1)

        tk.Label(motors, text="M2 Wrist",    font=("Segoe UI", 9, "bold")).grid(row=2, column=0, columnspan=2)
        make_btn(motors, "Up",   BITS["wrist_up"],   3, 0)
        make_btn(motors, "Down", BITS["wrist_down"], 3, 1)

        tk.Label(motors, text="M3 Elbow",    font=("Segoe UI", 9, "bold")).grid(row=4, column=0, columnspan=2)
        make_btn(motors, "Up",   BITS["elbow_up"],   5, 0)
        make_btn(motors, "Down", BITS["elbow_down"], 5, 1)

        tk.Label(motors, text="M4 Shoulder", font=("Segoe UI", 9, "bold")).grid(row=6, column=0, columnspan=2)
        make_btn(motors, "Up",   BITS["shoulder_up"],   7, 0)
        make_btn(motors, "Down", BITS["shoulder_down"], 7, 1)

        tk.Label(motors, text="M5 Base",     font=("Segoe UI", 9, "bold")).grid(row=8, column=0, columnspan=2)
        make_btn(motors, "Left",  BITS["base_left"],  9, 0)
        make_btn(motors, "Right", BITS["base_right"], 9, 1)

        tk.Button(motors, text="STOP ALL", width=26, height=2,
                  bg="#c0392b", fg="white", command=self.stop_all).grid(
                      row=10, column=0, columnspan=2, pady=8)

        self.led_btn = tk.Button(motors, text="LED OFF", width=26, command=self.toggle_led)
        self.led_btn.grid(row=11, column=0, columnspan=2, pady=2)

        presets_frame = tk.LabelFrame(main, text="Presets (timed sequences)", padx=8, pady=8)
        presets_frame.grid(row=0, column=1, sticky="n")

        self.status = tk.Label(presets_frame, text="Ready", fg="green", font=("Segoe UI", 9, "bold"))
        self.status.grid(row=0, column=0, columnspan=4, pady=(0, 6))

        for i in range(5):
            tk.Label(presets_frame, text=f"Preset {i+1}").grid(row=i+1, column=0, sticky="w", padx=4)
            tk.Button(presets_frame, text="Record", width=8,
                      command=lambda n=i: self.start_record(n)).grid(row=i+1, column=1, padx=2, pady=2)
            tk.Button(presets_frame, text="Play", width=8,
                      command=lambda n=i: self.play_preset(n)).grid(row=i+1, column=2, padx=2, pady=2)
            tk.Button(presets_frame, text="Clear", width=6,
                      command=lambda n=i: self.clear_preset(n)).grid(row=i+1, column=3, padx=2, pady=2)

        tk.Button(presets_frame, text="Stop Recording", width=28, bg="#e67e22", fg="white",
                  command=self.stop_record).grid(row=6, column=0, columnspan=4, pady=8)

        self.info = tk.Label(presets_frame, text="No presets recorded", fg="gray", wraplength=240, justify="left")
        self.info.grid(row=7, column=0, columnspan=4, pady=4)

    def on_press(self, bit):
        if self.playback_thread and self.playback_thread.is_alive():
            return
        self.arm.set_bit(bit, True)
        if self.recording_slot is not None:
            self.hold_starts[bit] = time.time()

    def on_release(self, bit):
        self.arm.set_bit(bit, False)
        if self.recording_slot is not None and bit in self.hold_starts:
            duration = time.time() - self.hold_starts.pop(bit)
            if duration > 0.05:
                self.presets[self.recording_slot].append((bit, duration))
                self.update_info()

    def stop_all(self):
        self.playback_stop.set()
        self.arm.stop_all()
        self.hold_starts.clear()

    def toggle_led(self):
        self.led_on = not self.led_on
        # Invert so button matches actual LED state
        self.arm.set_bit(BITS["led"], not self.led_on)
        self.led_btn.config(text="LED ON" if self.led_on else "LED OFF")

    def start_record(self, slot):
        if self.playback_thread and self.playback_thread.is_alive():
            messagebox.showwarning("Busy", "Stop playback first.")
            return
        self.recording_slot = slot
        self.presets[slot] = []
        self.hold_starts.clear()
        self.status.config(text=f"Recording Preset {slot+1}...", fg="red")
        self.update_info()

    def stop_record(self):
        if self.recording_slot is None:
            return
        now = time.time()
        for bit, start in list(self.hold_starts.items()):
            duration = now - start
            if duration > 0.05:
                self.presets[self.recording_slot].append((bit, duration))
            self.arm.set_bit(bit, False)
        self.hold_starts.clear()
        slot = self.recording_slot
        self.recording_slot = None
        self.status.config(text=f"Saved Preset {slot+1}", fg="green")
        self.update_info()

    def clear_preset(self, slot):
        self.presets[slot] = []
        self.update_info()
        if self.recording_slot == slot:
            self.stop_record()

    def update_info(self):
        lines = []
        for i, seq in enumerate(self.presets):
            if seq:
                total = sum(d for _, d in seq)
                lines.append(f"P{i+1}: {len(seq)} moves, {total:.1f}s")
            else:
                lines.append(f"P{i+1}: empty")
        self.info.config(text="\n".join(lines))

    def play_preset(self, slot):
        if not self.presets[slot]:
            messagebox.showinfo("Empty", f"Preset {slot+1} is empty.")
            return
        if self.recording_slot is not None:
            messagebox.showwarning("Busy", "Stop recording first.")
            return
        if self.playback_thread and self.playback_thread.is_alive():
            messagebox.showwarning("Busy", "Already playing.")
            return

        self.playback_stop.clear()
        self.playback_thread = threading.Thread(target=self._play, args=(slot,), daemon=True)
        self.playback_thread.start()

    def _play(self, slot):
        self.root.after(0, lambda: self.status.config(text=f"Playing Preset {slot+1}...", fg="blue"))
        try:
            for bit, duration in self.presets[slot]:
                if self.playback_stop.is_set():
                    break
                self.arm.set_bit(bit, True)
                end = time.time() + duration
                while time.time() < end:
                    if self.playback_stop.is_set():
                        break
                    time.sleep(0.05)
                self.arm.set_bit(bit, False)
                if self.playback_stop.is_set():
                    break
        finally:
            self.arm.stop_all()
            self.root.after(0, lambda: self.status.config(text="Ready", fg="green"))

    def on_close(self):
        self.playback_stop.set()
        self.arm.close()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    print("Starting KSR10 controller...")
    try:
        print("Looking for USB device...")
        arm = KSR10Controller()
        print("Device found and connected.")
    except Exception as e:
        print("ERROR:", e)
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Connection failed", str(e))
        except Exception as gui_err:
            print("Could not show error dialog:", gui_err)
        input("Press Enter to exit...")
        return

    print("Opening window...")
    App(arm).run()
    print("Window closed.")


if __name__ == "__main__":
    main()