#!/bin/bash
# BitFerry macOS 打包脚本
#
# 关键点: 必须用固定的自签名证书 "BitFerry Self-Signed" 重新签名,
# 否则 ad-hoc 签名每次重建都会让 cdhash 变化, macOS 的屏幕录制(截图)
# 授权随之失效, 用户被反复要求授权且无法截图。
#
# 该证书的 designated requirement 只依赖 bundle id + 证书指纹, 与 cdhash 无关,
# 所以授权一次后, 之后每次用同一证书重签都能保留权限。
#
# 首次在新机器上需要先创建证书(见 README / create_signing_cert.sh)。

set -e
cd "$(dirname "$0")"

CERT="BitFerry Self-Signed"
PY="${PYTHON:-.venv/bin/python3}"

echo "==> 检查签名证书..."
if ! security find-identity -v -p codesigning | grep -q "$CERT"; then
    echo "!! 未找到证书 \"$CERT\"。请先运行 ./create_signing_cert.sh 创建。"
    exit 1
fi

echo "==> 清理旧产物..."
# macOS 上 Spotlight/Finder 偶尔会在 rm 迭代时重建 .DS_Store 导致 "Directory not empty",
# 重试一次即可。
rm -rf build dist 2>/dev/null || { sleep 1; rm -rf build dist; }

echo "==> PyInstaller 打包..."
"$PY" -m PyInstaller bitferry.spec --noconfirm

echo "==> 用固定证书重新签名 (替换 ad-hoc)..."
codesign --force --deep --sign "$CERT" dist/BitFerry.app

echo "==> 验证签名..."
codesign --verify --deep --strict dist/BitFerry.app
codesign -d --requirements - dist/BitFerry.app 2>&1 | grep designated

echo "==> 去除本地隔离属性..."
xattr -dr com.apple.quarantine dist/BitFerry.app 2>/dev/null || true

echo "==> 打包 zip..."
( cd dist && rm -f ../BitFerry-macos-arm64.zip \
    && ditto -c -k --sequesterRsrc --keepParent BitFerry.app ../BitFerry-macos-arm64.zip )

echo "==> 完成: dist/BitFerry.app 与 BitFerry-macos-arm64.zip"
echo "   首次安装后需到 系统设置→隐私与安全性→屏幕录制 授权一次, 之后重建无需再授权。"
