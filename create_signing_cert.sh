#!/bin/bash
# 创建用于 BitFerry 的固定自签名代码签名证书 "BitFerry Self-Signed"。
# 只需在每台开发机上运行一次。运行后该证书会留在登录钥匙串里,
# build_mac.sh 用它签名, 使屏幕录制(截图)授权能跨重建保留。
#
# 过程中系统可能弹一次密码框, 输入开机密码即可。

set -e
CERT_NAME="BitFerry Self-Signed"

if security find-identity -v -p codesigning | grep -q "$CERT_NAME"; then
    echo "证书 \"$CERT_NAME\" 已存在, 无需重复创建。"
    exit 0
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "==> 生成自签名证书 (有效期 10 年)..."
openssl req -x509 -newkey rsa:2048 -keyout "$TMP/key.pem" -out "$TMP/cert.pem" \
    -days 3650 -nodes \
    -subj "/CN=$CERT_NAME/O=BitFerry" \
    -addext "extendedKeyUsage=codeSigning" \
    -addext "basicConstraints=critical,CA:false" \
    -addext "keyUsage=critical,digitalSignature"

openssl pkcs12 -export -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
    -out "$TMP/cert.p12" -passout pass:bitferry -name "$CERT_NAME"

echo "==> 导入登录钥匙串..."
security import "$TMP/cert.p12" -k ~/Library/Keychains/login.keychain-db \
    -P bitferry -A -T /usr/bin/codesign

echo "==> 设置代码签名信任 (可能弹密码框)..."
security add-trusted-cert -r trustRoot -p codeSign \
    -k ~/Library/Keychains/login.keychain-db "$TMP/cert.pem"

echo "==> 完成。当前代码签名身份:"
security find-identity -v -p codesigning | grep "$CERT_NAME"
