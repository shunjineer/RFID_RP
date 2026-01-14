# /src/driver/main_rp.py
# Python 3.11 / Flet 0.28.3
# Raspberry Pi 4B (RPi.GPIO, BCM)
# Desktop app: MR793200 SPI polling, showing Fan Speed (10 bars) & Seat Heater (3 bars)

import os
import sys
import time
import threading
from queue import Queue

import flet as ft

# インポートパス調整: /src/mr793200/mr793200_controller.py を読み込む
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.dirname(CURRENT_DIR)  # /src
if SRC_DIR not in sys.path:
    sys.path.append(SRC_DIR)

from mr793200.mr793200_controller import mr793200_controller  # noqa: E402

# Raspberry Pi GPIO
import RPi.GPIO as GPIO  # noqa: E402


# --------------- UI 構成用ユーティリティ ---------------

FAN_BAR_COUNT = 10
FAN_BAR_MIN_H = 24
FAN_BAR_MAX_H = 100

SEAT_BAR_COUNT = 3
SEAT_BAR_MIN_H = 18
SEAT_BAR_MAX_H = 42

# Fan bar の高さ（左→右で増加、下ぞろえで表示）
FAN_BAR_HEIGHTS = [
    FAN_BAR_MIN_H + (FAN_BAR_MAX_H - FAN_BAR_MIN_H) * i / (FAN_BAR_COUNT - 1)
    for i in range(FAN_BAR_COUNT)
]

# Seat bar の高さ（左→右で増加、下ぞろえで表示）
SEAT_BAR_HEIGHTS = [
    SEAT_BAR_MIN_H + (SEAT_BAR_MAX_H - SEAT_BAR_MIN_H) * i / (SEAT_BAR_COUNT - 1)
    for i in range(SEAT_BAR_COUNT)
]

# Fan bar 色（Seat OFF→LIGHT_BLUE、Seat ON→ORANGE）。右に行くほど濃く。
FAN_LIGHT_BLUE_PALETTE = [
    ft.Colors.LIGHT_BLUE_50,
    ft.Colors.LIGHT_BLUE_100,
    ft.Colors.LIGHT_BLUE_200,
    ft.Colors.LIGHT_BLUE_300,
    ft.Colors.LIGHT_BLUE_400,
    ft.Colors.LIGHT_BLUE_500,
    ft.Colors.LIGHT_BLUE_600,
    ft.Colors.LIGHT_BLUE_700,
    ft.Colors.LIGHT_BLUE_800,
    ft.Colors.LIGHT_BLUE_900,
]

FAN_ORANGE_PALETTE = [
    ft.Colors.ORANGE_100,
    ft.Colors.ORANGE_200,
    ft.Colors.ORANGE_300,
    ft.Colors.ORANGE_400,
    ft.Colors.ORANGE_500,
    ft.Colors.ORANGE_600,
    ft.Colors.ORANGE_700,
    ft.Colors.ORANGE_800,
    ft.Colors.ORANGE_900,
    ft.Colors.DEEP_ORANGE_ACCENT_700,
]

# Seat bar の色（右に行くほど濃く）
SEAT_ORANGE_PALETTE = [
    ft.Colors.ORANGE_300,
    ft.Colors.ORANGE_500,
    ft.Colors.ORANGE_700,
]


def make_fan_bars():
    """Fan Speed Bar（10本）コンテナ群を生成"""
    bars = []
    for i in range(FAN_BAR_COUNT):
        bars.append(
            ft.Container(
                width=20,
                height=FAN_BAR_HEIGHTS[i],
                bgcolor=ft.Colors.GREY_300,
                border_radius=4,
            )
        )
    return bars


def make_seat_bars():
    """Seat Heater Bar（3本）コンテナ群を生成"""
    bars = []
    for i in range(SEAT_BAR_COUNT):
        bars.append(
            ft.Container(
                width=20,
                height=SEAT_BAR_HEIGHTS[i],
                bgcolor=ft.Colors.GREY_300,
                border_radius=4,
            )
        )
    return bars


def set_fan_bars_color_and_active(bars, active_count, seat_level):
    """Fan bar の色設定。active_count 本を左からアクティブ化。
       seat_level=0 → LIGHT_BLUE、1..3 → ORANGE
       非アクティブは GREY_300。
    """
    palette = FAN_LIGHT_BLUE_PALETTE if seat_level == 0 else FAN_ORANGE_PALETTE
    for i in range(FAN_BAR_COUNT):
        bars[i].bgcolor = palette[i] if i < active_count else ft.Colors.GREY_300


def set_seat_bars_color_and_active(bars, level):
    """Seat bar の色設定。level 本を左からアクティブ化。
       アクティブは ORANGE 系（右ほど濃く）、非アクティブは GREY_300。
    """
    for i in range(SEAT_BAR_COUNT):
        bars[i].bgcolor = SEAT_ORANGE_PALETTE[i] if i < level else ft.Colors.GREY_300


def off_all_bars(seat_controls):
    """すべての席のバーをオフ（グレーアウト）"""
    for seat in seat_controls.values():
        # Fan
        for bar in seat["fan_bars"]:
            bar.bgcolor = ft.Colors.GREY_300
        # Seat
        for bar in seat["seat_bars"]:
            bar.bgcolor = ft.Colors.GREY_300


# --------------- SPI / GPIO 管理 ---------------

class MR793200Poller:
    """MR793200 を 1秒周期で USERメモリ(0x0422, 2 words) 読むポーラ"""

    def __init__(self, sclk_frequency=1_000_000):
        self.sclk_frequency = sclk_frequency
        self.controller = None
        self.stop_event = threading.Event()
        self.thread = None
        self.cleaned_up = False

        # 前回値（無効値時のフォールバック用）
        self.prev_fan = {"FL": 0, "FR": 0, "RL": 0, "RR": 0}
        self.prev_seat = {"FL": 0, "FR": 0, "RL": 0, "RR": 0}

    def setup_gpio(self):
        # BCM指定、GPIO27 を出力 High
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(27, GPIO.OUT, initial=GPIO.HIGH)
        GPIO.output(27, GPIO.HIGH)

    def safe_cleanup(self):
        if not self.cleaned_up:
            try:
                GPIO.cleanup(27)  # 指定PINのみ
            except Exception:
                pass
            try:
                if self.controller is not None and hasattr(self.controller, "spi"):
                    self.controller.spi.close()
            except Exception:
                pass
            self.cleaned_up = True

    def start(self, page: ft.Page, publish_fn):
        """ポーリング開始。publish_fn はメインスレッドへ渡すための関数（page.pubsub.send_all を想定）。"""
        self.stop_event.clear()
        self.cleaned_up = False

        # GPIO設定
        self.setup_gpio()
        # SPI コントローラ生成
        self.controller = mr793200_controller(sclk_frequency=self.sclk_frequency)

        # TID を 1回読み出し
        try:
            tid_hex = self.controller.read_nvm4(0x04, 0x16, 6).upper()
            print(tid_hex)
        except Exception:
            tid_hex = None

        # メインスレッドに TID 表示更新要求
        publish_fn({"type": "tid", "value": tid_hex})

        # バックグラウンドスレッド開始（UI更新は pubsub 経由でメインスレッドに渡す）
        def _run():
            try:
                while not self.stop_event.is_set():
                    try:
                        data_hex = self.controller.read_nvm4(0x04, 0x22, 2).upper()
                        # 期待長: 8 hex chars (2 words)
                        if not data_hex or len(data_hex) < 8:
                            raise ValueError("Invalid USER mem read length")

                        # 2ワード分を分割
                        w1 = int(data_hex[0:4], 16)  # 1ワード目：Fan Speed
                        w2 = int(data_hex[4:8], 16)  # 2ワード目：Seat Heater Level

                        # A. FL←[3:0], FR←[7:4], RL←[11:8], RR←[15:12]
                        fan_vals = {
                            "FL": (w1 >> 0) & 0xF,
                            "FR": (w1 >> 4) & 0xF,
                            "RL": (w1 >> 8) & 0xF,
                            "RR": (w1 >> 12) & 0xF,
                        }
                        seat_vals = {
                            "FL": (w2 >> 0) & 0xF,
                            "FR": (w2 >> 4) & 0xF,
                            "RL": (w2 >> 8) & 0xF,
                            "RR": (w2 >> 12) & 0xF,
                        }

                        # 無効値は前回値を使用
                        for k in fan_vals:
                            if 0xB <= fan_vals[k] <= 0xF:
                                fan_vals[k] = self.prev_fan[k]
                        for k in seat_vals:
                            if 0x4 <= seat_vals[k] <= 0xF:
                                seat_vals[k] = self.prev_seat[k]

                        # 前回値更新
                        self.prev_fan.update(fan_vals)
                        self.prev_seat.update(seat_vals)

                        # メインスレッドへ通知
                        publish_fn({"type": "update", "fan": fan_vals, "seat": seat_vals})

                    except Exception as e:
                        # 読み取り例外は継続（UIへはエラー表示は行わず、次周期で再試行）
                        # 必要ならログ等追加可能
                        pass

                    # 1秒周期
                    for _ in range(10):
                        if self.stop_event.is_set():
                            break
                        time.sleep(0.1)
            except Exception:
                # 想定外の例外でも終了処理は必ず実行
                self.safe_cleanup()
            finally:
                # スレッド終了時にも安全にクリーンアップ
                self.safe_cleanup()

        self.thread = threading.Thread(target=_run, daemon=True)
        self.thread.start()

    def stop(self):
        """ポーリング停止＋終了処理。例外の有無にかかわらず cleanup→SPI close の順で実行。"""
        try:
            self.stop_event.set()
            if self.thread is not None and self.thread.is_alive():
                self.thread.join(timeout=2.0)
        finally:
            self.safe_cleanup()


# --------------- Flet UI ---------------

def main(page: ft.Page):
    page.title = "MR793200 Driver Monitor"
    # page.window.width = 1920
    # page.window.height = 1080
    page.window.maximized = True
    page.padding = 20
    page.theme_mode = ft.ThemeMode.LIGHT

    # 画像パス（/src/driver/main_rp.py からの相対パス）
    fan_img_path = os.path.join(SRC_DIR, "driver/img/fan_fill.png")
    seat_img_path = os.path.join(SRC_DIR, "driver/img/seat_heated_fill.png")

    # 上部コンテナ: Play/Stop + TID
    play_btn = ft.IconButton(
        icon=ft.Icons.PLAY_CIRCLE_ROUNDED,
        icon_size=54,
        icon_color=ft.Colors.GREEN_ACCENT_400,
        tooltip="Start seat sensing",
        disabled=False,  # 初期値: Activate
        on_click=None,   # 後でセット
    )
    stop_btn = ft.IconButton(
        icon=ft.Icons.STOP_CIRCLE_ROUNDED,
        icon_size=54,
        icon_color=ft.Colors.GREY_400,
        tooltip="Stop seat sensing",
        disabled=True,  # 初期値: Deactivate（グレーアウト）
        on_click=None,  # 後でセット
    )

    tid_label = ft.Container(
        content=ft.Text(value="TID", size=20, weight=ft.FontWeight.W_600),
        bgcolor=ft.Colors.GREY_300,
        width=80,
        height=40,
        padding=ft.padding.only(left=20, top=5, bottom=5),
        margin=0,
    )
    tid_value_text = ft.Text(value="Push start button.", size=20)
    tid_value = ft.Container(
        content=tid_value_text,
        bgcolor=ft.Colors.BLUE_GREY_50,
        width=500,
        height=40,
        padding=5,
        margin=0,
    )

    upper_container = ft.Container(
        content=ft.Column(
            controls=[
                ft.Row(controls=[play_btn, stop_btn], spacing=10),
                ft.Row(controls=[tid_label, tid_value], spacing=0),
            ],
            spacing=15,
        )
    )

    # 下部コンテナ: 2x2 の席コンテナ群（FL, FR, RL, RR）
    seat_controls = {}
    def make_seat_container(name: str):
        # Fan
        fan_bars = make_fan_bars()
        fan_row = ft.Row(
            controls=[
                ft.Container(width=60),
                ft.Image(src=fan_img_path, width=50, height=50),
                ft.Row(controls=fan_bars, spacing=10, vertical_alignment=ft.CrossAxisAlignment.END),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.END,  # 下ぞろえ
        )

        # Seat
        seat_bars = make_seat_bars()
        seat_row = ft.Row(
            controls=[
                ft.Container(width=60),
                ft.Image(src=seat_img_path, width=50, height=50),
                ft.Row(controls=seat_bars, spacing=10, vertical_alignment=ft.CrossAxisAlignment.END),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.END,  # 下ぞろえ
        )

        container = ft.Container(
            bgcolor=ft.Colors.BLUE_GREY_50,
            border=ft.border.all(1, ft.Colors.GREY_300),
            border_radius=10,
            padding= ft.padding.only(left=100, right=100, top=10, bottom=10),
            content=ft.Column(
                controls=[
                    ft.Text(value=name, size=24, color=ft.Colors.GREY_700, weight=ft.FontWeight.BOLD),
                    ft.Row(
                        controls=[
                            ft.Container(width=60),
                            ft.Icon(ft.Icons.AIR, color=ft.Colors.LIGHT_BLUE_600),
                            ft.Text(value="Fan Speed", size=16),
                        ],
                    ),
                    fan_row,
                    ft.Divider(height=50, thickness=0.5),
                    ft.Row(
                        controls=[
                            ft.Container(width=60),
                            ft.Icon(ft.Icons.WAVES, color=ft.Colors.ORANGE_400),
                            ft.Text(value="Seat Heater", size=16),
                        ],
                    ),
                    ft.Container(height=10),
                    seat_row,
                ],
                spacing=8,
                # alignment=ft.Alignment(0.0, 0.0)
            ),
            width=700,
            height=380,
        )

        seat_controls[name] = {
            "container": container,
            "fan_bars": fan_bars,
            "seat_bars": seat_bars,
            "fan_value": 0,
            "seat_value": 0,
        }
        return container

    lower_container = ft.Container(
        content=ft.Column(
            controls=[
                ft.Row(controls=[make_seat_container("FL"), make_seat_container("FR")], spacing=10),
                ft.Row(controls=[make_seat_container("RL"), make_seat_container("RR")], spacing=10),
            ],
            spacing=10,
        )
    )

    # ページ構成
    page.add(
        ft.Column(
            controls=[upper_container, ft.Divider(), lower_container],
            spacing=20,
        )
    )

    # Poller 準備（UI更新はメインスレッドで page.update() を呼ぶ）
    poller = MR793200Poller(sclk_frequency=1_000_000)

    # pubsub: バックグラウンドからメインスレッドで UI 更新
    def on_pubsub_message(msg):
        # type: "tid" | "update"
        if not isinstance(msg, dict):
            return

        if msg.get("type") == "tid":
            tid_hex = msg.get("value")
            tid_value_text.value = tid_hex if tid_hex else "Push start button."
            page.update()
            return

        if msg.get("type") == "update":
            fan_vals = msg.get("fan", {})
            seat_vals = msg.get("seat", {})
            for name in ["FL", "FR", "RL", "RR"]:
                fv = int(fan_vals.get(name, 0))
                sv = int(seat_vals.get(name, 0))
                seat_controls[name]["fan_value"] = fv
                seat_controls[name]["seat_value"] = sv

                set_fan_bars_color_and_active(seat_controls[name]["fan_bars"], fv, sv)
                set_seat_bars_color_and_active(seat_controls[name]["seat_bars"], sv)

            page.update()

    page.pubsub.subscribe(on_pubsub_message)

    # ボタンハンドラ
    def on_play_click(e):
        # UI: Play→Deactivate, Stop→Activate
        play_btn.disabled = True
        play_btn.icon_color = ft.Colors.GREY_400
        stop_btn.disabled = False
        stop_btn.icon_color = ft.Colors.RED
        page.update()

        # 通信開始（UI更新は pubsub 経由で行う。page.update() はメインスレッドのみ）
        poller.start(page, publish_fn=page.pubsub.send_all)

    def on_stop_click(e):
        # UI: Stop→Deactivate
        stop_btn.disabled = True
        stop_btn.icon_color = ft.Colors.GREY_400
        play_btn.disabled = False
        play_btn.icon_color = ft.Colors.GREEN_ACCENT_400
        page.update()

        # 終了処理（例外有無に関わらず cleanup→SPI close）
        poller.stop()

        # UI: バーをオフ、TID クリア、Play→Activate
        off_all_bars(seat_controls)
        tid_value_text.value = "Push start button."
        play_btn.disabled = False
        page.update()

    play_btn.on_click = on_play_click
    stop_btn.on_click = on_stop_click

    # ページクローズ時にも安全に停止
    def on_close(e):
        try:
            poller.stop()
        finally:
            # 何もしなくても Flet が閉じる
            pass

    page.on_close = on_close


if __name__ == "__main__":
    ft.app(target=main)
