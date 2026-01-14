# /src/battery/main_rp.py
# Python 3.11
# Flet 0.28.3
# Desktop app for Raspberry Pi 4B (Debian Trixie)
# - GPIO4: input with pull-down (VDET), polled every 500 ms
# - GPIO15: output reset, low initially. If VDET goes high => after 100ms set high. If VDET low => set low immediately.
# - After GPIO15 set high, wait 100ms then initialize I2C (smbus2) to PCA9539PWR (0x74).
# - PCA9539 initialization: config as outputs (0x06/0x07=0x00), polarity 0x04/0x05=0x00, outputs 0x02/0x03=0x00 and verify by reading.
# - SPI (MR793200) read every 500ms on Play; Stop cleans up and resets GUI and PCA9539 outputs.
# - UI built with Flet, no Expanded/GridView, only page.update() for rendering.
# - Images are read from disk and converted to base64. Prefer resizing to 180x180 via Pillow; fallback to original size if Pillow unavailable.

import asyncio
import base64
import io
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

import flet as ft
from flet import Colors, Icons

# GPIO / I2C
try:
    import RPi.GPIO as GPIO
except Exception as e:
    print(f"[GPIO] Import error: {e}")
    raise

try:
    from smbus2 import SMBus, i2c_msg
except Exception as e:
    print(f"[I2C] Import error (smbus2): {e}")
    raise

# Add project /src to sys.path and import MR793200 controller
try:
    SRC_DIR = Path(__file__).resolve().parents[1]
    if str(SRC_DIR) not in sys.path:
        sys.path.append(str(SRC_DIR))
    from mr793200.mr793200_controller import mr793200_controller
except Exception as e:
    print(f"[MR793200] Import error: {e}")
    raise

# Optional Pillow for actual image resizing
try:
    from PIL import Image  # type: ignore
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False
    print("[Image] Pillow not available. Will not resize images physically; using original data and scaling by control size.")

# -----------------------------
# Constants
# -----------------------------
PIN_VDET = 4   # GPIO4 (BCM) physical pin 7
PIN_RESET = 15 # GPIO15 (BCM) physical pin 10
PIN_SPI_EN = 27  # GPIO27 (BCM) physical pin 13

I2C_BUS_NO = 1
PCA9539_ADDR = 0x74

# PCA9539 registers
REG_OUT0 = 0x02  # Output port 0 (P00..P07)
REG_OUT1 = 0x03  # Output port 1 (P10..P17)
REG_POL0 = 0x04  # Polarity inversion 0
REG_POL1 = 0x05  # Polarity inversion 1
REG_CFG0 = 0x06  # Configuration 0
REG_CFG1 = 0x07  # Configuration 1

# MR793200 USER memory address map for SPI reads
USER_ADDRS = [0x22, 0x24, 0x26, 0x28, 0x2A, 0x2C, 0x2E, 0x30, 0x32]

# -----------------------------
# Utility: image loader as base64 with optional resizing
# -----------------------------
def load_image_base64(image_path: Path, size: Tuple[int, int] = (180, 180)) -> str:
    """
    Loads an image from disk and returns base64 string.
    If Pillow is available, will resize to given size before encoding.
    """
    try:
        if PIL_AVAILABLE:
            with Image.open(image_path) as img:
                img = img.convert("RGBA")
                img = img.resize(size, Image.LANCZOS)
                buffer = io.BytesIO()
                img.save(buffer, format="PNG")
                b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
                return b64
        else:
            # Fallback: no resizing, just raw file to base64
            data = image_path.read_bytes()
            return base64.b64encode(data).decode("utf-8")
    except Exception as e:
        print(f"[Image] Failed to load/resize image {image_path}: {e}")
        # Return empty string to avoid crash; UI will show broken image
        return ""

# -----------------------------
# PCA9539 Controller
# -----------------------------
class PCA9539Controller:
    def __init__(self, bus_no: int = I2C_BUS_NO, addr: int = PCA9539_ADDR):
        self.addr = addr
        self.bus_no = bus_no
        self.bus: Optional[SMBus] = None
        self.initialized = False

    def open(self):
        if self.bus is None:
            try:
                self.bus = SMBus(self.bus_no)
                print(f"[I2C] Opened SMBus({self.bus_no})")
            except Exception as e:
                print(f"[I2C] Failed to open bus {self.bus_no}: {e}")
                self.bus = None
                raise

    def write_reg(self, reg: int, val: int):
        if self.bus is None:
            raise RuntimeError("I2C bus not open")
        try:
            self.bus.write_byte_data(self.addr, reg, val & 0xFF)
        except Exception as e:
            print(f"[I2C] Write reg 0x{reg:02X} failed: {e}")
            raise

    def read_reg(self, reg: int) -> int:
        if self.bus is None:
            raise RuntimeError("I2C bus not open")
        try:
            return self.bus.read_byte_data(self.addr, reg) & 0xFF
        except Exception as e:
            print(f"[I2C] Read reg 0x{reg:02X} failed: {e}")
            raise

    def init_device(self) -> bool:
        """
        Initialize PCA9539:
        - Set polarity 0x04,0x05 = 0x00
        - Set config 0x06,0x07 = 0x00 (all outputs)
        - Set outputs 0x02,0x03 = 0x00
        Verify each write by reading back.
        """
        try:
            self.open()
            # Polarity
            self.write_reg(REG_POL0, 0x00)
            self.write_reg(REG_POL1, 0x00)
            pol0 = self.read_reg(REG_POL0)
            pol1 = self.read_reg(REG_POL1)

            # Config as outputs
            self.write_reg(REG_CFG0, 0x00)
            self.write_reg(REG_CFG1, 0x00)
            cfg0 = self.read_reg(REG_CFG0)
            cfg1 = self.read_reg(REG_CFG1)

            # Outputs low
            self.write_reg(REG_OUT0, 0x00)
            self.write_reg(REG_OUT1, 0x00)
            out0 = self.read_reg(REG_OUT0)
            out1 = self.read_reg(REG_OUT1)

            ok = (pol0 == 0x00 and pol1 == 0x00 and
                  cfg0 == 0x00 and cfg1 == 0x00 and
                  out0 == 0x00 and out1 == 0x00)
            self.initialized = ok
            print(f"[I2C] PCA9539 init {'OK' if ok else 'NG'} (pol={pol0:02X}/{pol1:02X}, cfg={cfg0:02X}/{cfg1:02X}, out={out0:02X}/{out1:02X})")
            return ok
        except Exception as e:
            print(f"[I2C] PCA9539 init failed: {e}")
            self.initialized = False
            return False

    def set_outputs_16(self, mask16: int) -> bool:
        """
        Set 16 outputs:
        No.1->bit0 (P00) ... No.8->bit7 (P07), No.9->bit8 (P10) ... No.16->bit15 (P17).
        """
        if self.bus is None or not self.initialized:
            # Not initialized => cannot write outputs
            print("[I2C] Outputs not set: bus not open or device not initialized.")
            return False
        out0 = mask16 & 0xFF        # P0 bank
        out1 = (mask16 >> 8) & 0xFF # P1 bank
        try:
            self.write_reg(REG_OUT0, out0)
            self.write_reg(REG_OUT1, out1)
            # Read back to verify
            r0 = self.read_reg(REG_OUT0)
            r1 = self.read_reg(REG_OUT1)
            ok = (r0 == out0 and r1 == out1)
            if not ok:
                print(f"[I2C] Output verify NG: wrote {out0:02X}/{out1:02X} read {r0:02X}/{r1:02X}")
            return ok
        except Exception as e:
            print(f"[I2C] set_outputs_16 failed: {e}")
            return False

    def shutdown_safe(self):
        """
        App exit: 1) outputs low 2) close bus
        """
        try:
            if self.bus is not None:
                try:
                    # Ensure outputs low
                    self.write_reg(REG_OUT0, 0x00)
                    self.write_reg(REG_OUT1, 0x00)
                    print("[I2C] Outputs forced LOW on shutdown.")
                except Exception as e:
                    print(f"[I2C] Failed to force outputs LOW: {e}")
        finally:
            try:
                if self.bus is not None:
                    self.bus.close()
                    print("[I2C] Bus closed.")
            except Exception as e:
                print(f"[I2C] Bus close error: {e}")
            self.bus = None
            self.initialized = False

# -----------------------------
# App State
# -----------------------------
class AppState:
    def __init__(self):
        # GPIO states
        self.vdet_state: Optional[bool] = None
        self.reset_state: Optional[bool] = None

        # I2C controller
        self.pca = PCA9539Controller()
        self.i2c_ready: bool = False

        # SPI controller and task
        self.spi_ctrl: Optional[mr793200_controller] = None
        self.spi_task: Optional[asyncio.Task] = None
        self.spi_running: bool = False

        # Flags / controls
        self.app_running: bool = True

        # Images base64
        self.b64_light_on: str = ""
        self.b64_light_off: str = ""
        self.b64_battery: str = ""

        # UI references
        self.play_btn: Optional[ft.IconButton] = None
        self.stop_btn: Optional[ft.IconButton] = None
        self.items_on_images: List[ft.Image] = []
        self.items_temp_texts: List[ft.Text] = []

        # Lower status texts
        self.i2c_info_text: Optional[ft.Text] = None
        self.vdet_info_text: Optional[ft.Text] = None
        self.reset_info_text: Optional[ft.Text] = None
        self.hot_reset_notice: Optional[ft.Text] = None

    def load_images(self, base_dir: Path):
        img_on = base_dir / "img" / "light_on.png"
        img_off = base_dir / "img" / "light_off.png"
        img_battery = base_dir / "img" / "battery.png"
        self.b64_light_on = load_image_base64(img_on, (180, 180))
        self.b64_light_off = load_image_base64(img_off, (180, 180))
        self.b64_battery = load_image_base64(img_battery, (180, 180))

# -----------------------------
# GPIO helpers
# -----------------------------
def gpio_setup_initial():
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        # GPIO4: input with pull-down
        GPIO.setup(PIN_VDET, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
        # GPIO15: output, initial LOW
        GPIO.setup(PIN_RESET, GPIO.OUT, initial=GPIO.LOW)
        print("[GPIO] Initialized: VDET as input (PUD_DOWN), RESET as output LOW.")
    except Exception as e:
        print(f"[GPIO] Setup error: {e}")
        raise

def gpio_set_reset(level: bool):
    try:
        GPIO.output(PIN_RESET, GPIO.HIGH if level else GPIO.LOW)
    except Exception as e:
        print(f"[GPIO] Set RESET {'HIGH' if level else 'LOW'} error: {e}")

def gpio_get_vdet() -> bool:
    try:
        return GPIO.input(PIN_VDET) == GPIO.HIGH
    except Exception as e:
        print(f"[GPIO] Read VDET error: {e}")
        return False

def gpio_setup_spi_en_high():
    try:
        GPIO.setup(PIN_SPI_EN, GPIO.OUT, initial=GPIO.HIGH)
        print("[GPIO] SPI_EN set HIGH.")
    except Exception as e:
        print(f"[GPIO] SPI_EN setup error: {e}")
        raise

def gpio_cleanup_spi_en():
    try:
        GPIO.cleanup(PIN_SPI_EN)
        print("[GPIO] SPI_EN cleaned up.")
    except Exception as e:
        print(f"[GPIO] SPI_EN cleanup error: {e}")

def gpio_cleanup_vdet_reset():
    try:
        GPIO.cleanup([PIN_VDET, PIN_RESET])
        print("[GPIO] Cleaned up VDET and RESET.")
    except Exception as e:
        print(f"[GPIO] Cleanup VDET/RESET error: {e}")

# -----------------------------
# SPI reader task
# -----------------------------
async def spi_reader_task(page: ft.Page, state: AppState):
    """
    Play starts this task. It:
    - sets GPIO27 high
    - instantiates mr793200_controller
    - loops every 500ms: reads MR793200 user memory, updates GUI, and updates PCA9539 outputs
    """
    try:
        gpio_setup_spi_en_high()
        try:
            state.spi_ctrl = mr793200_controller(sclk_frequency=1_000_000)
        except Exception as e:
            print(f"[SPI] MR793200 controller init failed: {e}")
            # Ensure cleanup even on failure
            gpio_cleanup_spi_en()
            return

        state.spi_running = True
        while state.spi_running:
            try:
                # 0x22 -> On/Off 16bits
                hex_22 = state.spi_ctrl.read_nvm1(0x04, 0x22, 1)
                on_off_word = int(hex_22, 16) & 0xFFFF

                # Temperature words
                temp_words = {}
                for addr in USER_ADDRS[1:]:  # skip 0x22
                    hex_val = state.spi_ctrl.read_nvm1(0x04, addr, 1)
                    temp_words[addr] = int(hex_val, 16) & 0xFFFF

                # Update UI and compose I2C outputs
                for idx in range(16):
                    on_bit = (on_off_word >> idx) & 0x1
                    # Update light image
                    img_ctrl = state.items_on_images[idx]
                    img_ctrl.src_base64 = state.b64_light_on if on_bit else state.b64_light_off

                    # Determine temperature for No. (1-based)
                    no = idx + 1
                    # Map to address and byte position
                    # Pairs: (1,2)->0x24, (3,4)->0x26, ... (15,16)->0x32
                    pair_index = (no - 1) // 2
                    addr = 0x24 + 2 * pair_index
                    word = temp_words.get(addr, 0)
                    if no % 2 == 1:
                        temp_val = word & 0xFF
                    else:
                        temp_val = (word >> 8) & 0xFF
                    temp_text_ctrl = state.items_temp_texts[idx]
                    temp_text_ctrl.value = f"{temp_val}°C"

                # Update PCA9539 outputs according to On/Off mask
                if state.pca.initialized and state.i2c_ready and state.vdet_state and state.reset_state:
                    ok = state.pca.set_outputs_16(on_off_word)
                    if not ok:
                        print("[I2C] Warning: PCA9539 outputs update verification failed.")

                page.update()
            except Exception as e:
                print(f"[SPI] Loop error: {e}")

            await asyncio.sleep(0.5)

    except Exception as e:
        print(f"[SPI] Task fatal error: {e}")
    finally:
        # SPI termination sequence:
        # 1) GPIO27 cleanup
        gpio_cleanup_spi_en()
        # 2) Close SPI port
        try:
            if state.spi_ctrl is not None and hasattr(state.spi_ctrl, "spi"):
                try:
                    state.spi_ctrl.spi.close()
                    print("[SPI] SPI port closed.")
                except Exception as ee:
                    print(f"[SPI] SPI close error: {ee}")
            state.spi_ctrl = None
        except Exception as e:
            print(f"[SPI] Finalize error: {e}")

# -----------------------------
# GPIO poll task (VDET/RESET + I2C init)
# -----------------------------
async def gpio_poll_task(page: ft.Page, state: AppState):
    """
    Polls VDET every 500ms. On rising edge:
    - after 100ms set RESET high;
    - after another 100ms initialize I2C and verify.
    On falling edge:
    - set RESET low immediately.
    Updates Lower container info labels accordingly.ft.Icons
    """
    while state.app_running:
        try:
            vdet_now = gpio_get_vdet()
            # Determine reset state by reading the current output (we track state.reset_state)
            # But GPIO library has no readback for output easily; we keep internal state.
            if state.vdet_state is None:
                state.vdet_state = vdet_now

            if vdet_now != state.vdet_state:
                # Edge detected
                state.vdet_state = vdet_now
                if vdet_now:
                    # Rising edge: after 100ms set RESET high
                    await asyncio.sleep(0.1)
                    gpio_set_reset(True)
                    state.reset_state = True
                    # After 100ms, initialize I2C
                    await asyncio.sleep(0.1)
                    if not state.pca.initialized:
                        ok = state.pca.init_device()
                        state.i2c_ready = ok
                    else:
                        state.i2c_ready = True
                else:
                    # Falling edge: set RESET low immediately
                    gpio_set_reset(False)
                    state.reset_state = False
                    # Keep I2C opened but mark status by GPIO
                # Update info texts after edge handling
                update_lower_info(page, state)

            else:
                # No edge, but still maintain info
                update_lower_info(page, state)

        except Exception as e:
            print(f"[GPIO] Poll error: {e}")

        await asyncio.sleep(0.5)

def update_lower_info(page: ft.Page, state: AppState):
    # VDET info text
    if state.vdet_info_text is not None:
        if state.vdet_state is True:
            state.vdet_info_text.value = "High"
        elif state.vdet_state is False:
            state.vdet_info_text.value = "Low"
        else:
            state.vdet_info_text.value = "-"

    # RESET info text
    if state.reset_info_text is not None:
        if state.reset_state is True:
            state.reset_info_text.value = "High"
        elif state.reset_state is False:
            state.reset_info_text.value = "Low"
        else:
            state.reset_info_text.value = "-"

    # I2C Status info per specified conditions
    if state.i2c_info_text is not None:
        if (state.vdet_state is False) and (state.reset_state is False):
            state.i2c_info_text.value = "Not initialized."
            state.i2c_info_text.color = None
        elif (state.vdet_state is True) and (state.reset_state is False):
            state.i2c_info_text.value = "Waiting reset released..."
            state.i2c_info_text.color = None
        elif (state.vdet_state is True) and (state.reset_state is True):
            state.i2c_info_text.value = "Succeeded."
            state.i2c_info_text.color = Colors.GREEN
        else:
            # Any other undefined combo
            state.i2c_info_text.value = "-"
            state.i2c_info_text.color = None

    page.update()

# -----------------------------
# UI Builders
# -----------------------------
def build_no_cell(state: AppState, no_index: int) -> ft.Container:
    """
    Build a "No. X" cell with:
    - Text "No. X"
    - Image (on/off)
    - Battery stack with temperature text centered
    """
    title = ft.Text(f"No. {no_index}", size=16, weight=ft.FontWeight.W_600)
    img_onoff = ft.Image(src_base64=state.b64_light_off, width=120, height=120)
    state.items_on_images.append(img_onoff)

    temp_text = ft.Text("-°C", size=16, weight=ft.FontWeight.W_600, color=Colors.BLACK)
    state.items_temp_texts.append(temp_text)

    battery_stack = ft.Stack(
        width=120,
        height=120,
        controls=[
            ft.Image(src_base64=state.b64_battery, width=120, height=120),
            ft.Container(
                content=temp_text,
                width=120,
                height=120,
                alignment=ft.alignment.center,
                bgcolor=None,
                padding=0,
                margin=0,
            ),
        ],
    )

    col = ft.Column(
        controls=[title, img_onoff, battery_stack],
        spacing=6,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
    )
    return ft.Container(
        content=col,
        padding=6,
        border=ft.border.all(1, ft.Colors.GREY_300),
        bgcolor=ft.Colors.GREY_50,
        height=300,
        width=220,
        border_radius=10,
    )

def build_middle_grid(state: AppState) -> ft.Column:
    # Build 2 rows x 8 columns (no GridView)
    row1_cells = [build_no_cell(state, i) for i in range(1, 9)]
    row2_cells = [build_no_cell(state, i) for i in range(9, 17)]

    row1 = ft.Row(controls=row1_cells, spacing=4, alignment=ft.MainAxisAlignment.START)
    row2 = ft.Row(controls=row2_cells, spacing=4, alignment=ft.MainAxisAlignment.START)

    return ft.Column(controls=[row1, row2], spacing=4)

# -----------------------------
# Event Handlers
# -----------------------------
def on_play_click(e: ft.ControlEvent, page: ft.Page, state: AppState):
    # Deactivate Play, Activate Stop
    if state.play_btn:
        state.play_btn.disabled = True
        state.play_btn.icon_color = Colors.GREY_400
    if state.stop_btn:
        state.stop_btn.disabled = False
        state.stop_btn.icon_color = Colors.RED
    page.update()
    # Start SPI task
    if state.spi_task is None or state.spi_task.done():
        state.spi_task = page.run_task(spi_reader_task, page, state)

def on_stop_click(e: ft.ControlEvent, page: ft.Page, state: AppState):
    # Deactivate Stop
    if state.stop_btn:
        state.stop_btn.disabled = True
        state.stop_btn.icon_color = Colors.GREY_400
    page.update()

    async def do_stop():
        try:
            # Request SPI task stop
            state.spi_running = False
            # Wait for task to finish up to 1.5s
            t0 = time.time()
            while state.spi_task is not None and not state.spi_task.done():
                await asyncio.sleep(0.05)
                if time.time() - t0 > 1.5:
                    break
        except Exception as e2:
            print(f"[STOP] Waiting SPI task error: {e2}")

        # Reset UI: all Off and "-°C"
        try:
            for i in range(16):
                state.items_on_images[i].src_base64 = state.b64_light_off
                state.items_temp_texts[i].value = "-°C"
            page.update()
        except Exception as e3:
            print(f"[STOP] UI reset error: {e3}")

        # Force PCA9539 outputs off
        try:
            if state.pca.initialized:
                ok = state.pca.set_outputs_16(0x0000)
                if not ok:
                    print("[STOP] PCA9539 outputs OFF verify failed.")
        except Exception as e4:
            print(f"[STOP] PCA9539 off error: {e4}")

        # Activate Play again
        try:
            if state.play_btn:
                state.play_btn.disabled = False
                state.play_btn.icon_color = Colors.GREEN_ACCENT_400
            page.update()
        except Exception as e5:
            print(f"[STOP] Enable Play error: {e5}")

    page.run_task(do_stop)

def on_hot_reset_click(e: ft.ControlEvent, page: ft.Page, state: AppState):
    # If VDET high => toggle RESET low for 500ms then high
    vdet_now = gpio_get_vdet()
    if vdet_now:
        async def do_hot_reset():
            try:
                gpio_set_reset(False)
                state.reset_state = False
                update_lower_info(page, state)
                await asyncio.sleep(0.5)
                gpio_set_reset(True)
                state.reset_state = True
                update_lower_info(page, state)
                # After 100ms, ensure I2C initialized
                await asyncio.sleep(0.1)
                if not state.pca.initialized:
                    ok = state.pca.init_device()
                    state.i2c_ready = ok
                else:
                    state.i2c_ready = True
                update_lower_info(page, state)
            except Exception as ex:
                print(f"[Hot Reset] Error: {ex}")
        page.run_task(do_hot_reset)
        # Clear any notice if was shown
        if state.hot_reset_notice:
            state.hot_reset_notice.value = ""
            page.update()
    else:
        # Keep RESET low and show notice
        gpio_set_reset(False)
        state.reset_state = False
        if state.hot_reset_notice:
            state.hot_reset_notice.value = 'Available when VDET is "High".'
            state.hot_reset_notice.color = Colors.RED
        update_lower_info(page, state)

def on_window_event(e: ft.ControlEvent, page: ft.Page, state: AppState):
    if e.data == "close":
        # Prevent default close to do cleanup
        page.window_prevent_close = True

        async def do_cleanup_and_close():
            try:
                # Stop tasks
                state.app_running = False
                # Stop SPI
                state.spi_running = False
                if state.spi_task is not None:
                    t0 = time.time()
                    while not state.spi_task.done() and (time.time() - t0) < 1.5:
                        await asyncio.sleep(0.05)
                # I2C safe shutdown
                try:
                    state.pca.shutdown_safe()
                except Exception as e1:
                    print(f"[EXIT] PCA shutdown error: {e1}")
                # GPIO cleanup for VDET/RESET
                try:
                    gpio_cleanup_vdet_reset()
                except Exception as e2:
                    print(f"[EXIT] GPIO cleanup error: {e2}")
            finally:
                # Close window
                page.window_destroy()

        page.run_task(do_cleanup_and_close)

# -----------------------------
# Main UI
# -----------------------------
def main(page: ft.Page):
    page.title = "Battery Monitor (Raspberry Pi / MR793200 / PCA9539)"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.padding = 10
    page.spacing = 8
    page.window.maximized = True

    # Prepare state
    state = AppState()

    # Load images as base64
    base_dir = Path(__file__).resolve().parent
    state.load_images(base_dir)

    # Init GPIO base
    try:
        gpio_setup_initial()
        state.vdet_state = gpio_get_vdet()
        state.reset_state = False
    except Exception as e:
        print(f"[INIT] GPIO init error: {e}")

    # Upper controls: Play / Stop
    play_btn = ft.IconButton(
        icon=Icons.PLAY_CIRCLE_ROUNDED,
        icon_color=Colors.GREEN_ACCENT_400,
        icon_size=54,
        disabled=False,
        tooltip="Start battery monitoring",
        on_click=lambda e: on_play_click(e, page, state),
    )
    stop_btn = ft.IconButton(
        icon=Icons.STOP_CIRCLE_ROUNDED,
        icon_color=Colors.GREY_400,
        icon_size=54,
        disabled=True,  # Deactivated initially
        tooltip="Stop battery monitoring",
        on_click=lambda e: on_stop_click(e, page, state),
    )
    state.play_btn = play_btn
    state.stop_btn = stop_btn

    upper_row = ft.Row(
        controls=[play_btn, stop_btn],
        alignment=ft.MainAxisAlignment.START,
        spacing=12,
    )
    upper_container = ft.Container(content=upper_row)

    # Middle grid
    middle_column = build_middle_grid(state)

    # Lower section
    # I2C Status row
    i2c_status_label = ft.Container(
        content=ft.Text("I2C Status", weight=ft.FontWeight.W_600),
        width=160,
        height=40,
        bgcolor=Colors.GREY_300,
        padding=8,
        margin=0,
    )
    i2c_info_text = ft.Text("Not initialized.")
    state.i2c_info_text = i2c_info_text
    i2c_info_box = ft.Container(
        content=i2c_info_text,
        width=420,
        height=40,
        bgcolor=Colors.GREY_50,
        padding=8,
        margin=0,
        expand=False,
    )
    i2c_row = ft.Row(controls=[i2c_status_label, i2c_info_box], spacing=0)

    # VDET row
    vdet_label = ft.Container(
        content=ft.Text("VDET", weight=ft.FontWeight.W_600),
        width=160,
        height=40,
        bgcolor=Colors.GREY_300,
        padding=8,
        margin=0,
    )
    vdet_info_text = ft.Text("-")
    state.vdet_info_text = vdet_info_text
    vdet_info_box = ft.Container(
        content=vdet_info_text,
        width=420,
        height=40,
        bgcolor=Colors.GREY_50,
        padding=8,
        margin=0,
    )
    vdet_row = ft.Row(controls=[vdet_label, vdet_info_box], spacing=0)

    # RESET row
    reset_label = ft.Container(
        content=ft.Text("RESET", weight=ft.FontWeight.W_600),
        width=160,
        height=40,
        bgcolor=Colors.GREY_300,
        padding=8,
        margin=0,
    )
    reset_info_text = ft.Text("-")
    state.reset_info_text = reset_info_text
    reset_info_box = ft.Container(
        content=reset_info_text,
        width=420,
        height=40,
        bgcolor=Colors.GREY_50,
        padding=8,
        margin=0,
    )
    reset_row = ft.Row(controls=[reset_label, reset_info_box], spacing=0)

    # Hot Reset button and notice
    state.hot_reset_notice = ft.Text("", color=Colors.RED)
    hot_reset_btn = ft.ElevatedButton(
        text="Hot Reset",
        on_click=lambda e: on_hot_reset_click(e, page, state),
    )
    hot_reset_row = ft.Row(
        controls=[hot_reset_btn, state.hot_reset_notice],
        spacing=10,
        alignment=ft.MainAxisAlignment.START,
    )

    lower_column = ft.Column(
        controls=[i2c_row, vdet_row, reset_row, hot_reset_row],
        spacing=6,
    )

    # Build page layout: Upper, Divider, Middle, Divider, Lower
    page.add(
        upper_container,
        ft.Divider(),
        middle_column,
        ft.Divider(),
        lower_column,
    )

    # Start GPIO poll task
    page.run_task(gpio_poll_task, page, state)

    # Close handling
    page.window_prevent_close = True
    page.on_window_event = lambda e: on_window_event(e, page, state)

    # Initial lower info update
    update_lower_info(page, state)

if __name__ == "__main__":
    # flet desktop app
    ft.app(target=main)