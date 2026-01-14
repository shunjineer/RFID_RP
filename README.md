# Raspberry Pi 4B (OS: Trixie)への導入手順
## デバイスファイルの確認
```
ls -l /dev/i2c-1
```
でデバイスファイルが無ければ次の「I2Cの有効化」へ。

## I2Cの有効化
```
sudo raspi-config
# Interface Options -> I2C -> Enable
sudo reboot
```
再起動後もう一度以下実施すればOKなはず。
```
ls -l /dev/i2c-1
```

## バスの動作確認
i2c-tools を使うとバス・接続デバイスが見えるか簡単に確認可能
```
sudo apt update
sudo apt install -y i2c-tools

# バス1をスキャン(非rootでもi2cグループに入っていれば動作する)
i2cdetect -y 1
```
PCA9539PWRが接続・電源投入済みで、リセット解除されていて、プルアップが正しく入っている場合は、表に 74 が表示される(スレーブアドレス0x74)。

Pythonで確認する場合は以下スクリプトを使う。
```
# test_i2c.py
from smbus2 import SMBus
try:
    bus = SMBus(1)  # /dev/i2c-1
    print("I2C bus opened OK")
    bus.close()
except Exception as e:
    print("I2C open failed:", e)
```


## uvを使った環境構築

1) uv をインストール
- `curl -Ls https://astral.sh/uv/install.sh | sh`
- PATH 追加（必要なら）
  - `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc`
  - `exec $SHELL`

2) Python 3.11 の用意（uv が自動で取得・管理）
- 明示的に取得したい場合: `uv python install 3.11`
- インストール済みの確認: `uv python list`

3) プロジェクトフォルダ(ここではRFID_RP)を任意の場所にコピーしターミナルで移動
- `cd RFID_RP`

4) 依存の同期
- プロジェクトのルートで: `uv sync`
- これで .venv が生成され、uv.lock に固定された依存関係がインストールされます。
- プロジェクトルートに以下があること
  - .python-version
  - pyproject.toml
  - uv.lock
  - /src

5) 実行
- シートセンシング: `uv run flet run ./src/driver/main_rp.py`
- EVバッテリーセンシング: `uv run flet run ./src/battery/main_rp.py`


## VSCodeのインストール
Microsoft 公式リポジトリからインストール
- 事前確認
  - `uname -m` で aarch64 であることを確認
  - `sudo apt update` でパッケージ情報を最新化

- リポジトリ追加とインストール
  - `sudo apt install -y wget gpg apt-transport-https`
  - `wget -qO- https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor | sudo tee /usr/share/keyrings/packages.microsoft.gpg > /dev/null`
  - `echo 'deb [arch=arm64 signed-by=/usr/share/keyrings/packages.microsoft.gpg] https://packages.microsoft.com/repos/code stable main' | sudo tee /etc/apt/sources.list.d/vscode.list`
  - `sudo apt update`
  - `sudo apt install -y code`

- 起動
  - メニューから「Visual Studio Code」
  - もしくはターミナルで code

トラブル対処のヒント
- 画面が真っ黒・クラッシュする場合
  - `code --disable-gpu` で起動してみる（GPU ドライバ相性回避）
- 日本語フォントが欠ける場合
  - `sudo apt install -y fonts-noto-cjk fonts-noto-color-emoji`

