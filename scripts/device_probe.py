#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
S2 设备探测：品牌 / 型号 / 系统 / 微信版本 / 存储 / root 状态，
并根据品牌给出回传方案建议。

用法:
    python device_probe.py [--adb <adb路径>] [--serial <设备序列号>]
"""
import argparse
import re
import subprocess
import sys

BRAND_MAP = {
    'xiaomi': ('小米 / Redmi', 'B', 'MIUI 本地备份手术（主路径，已实战验证）'),
    'redmi': ('小米 / Redmi', 'B', 'MIUI 本地备份手术（主路径，已实战验证）'),
    'poco': ('小米 / POCO', 'B', 'MIUI 本地备份手术（主路径，已实战验证）'),
    'huawei': ('华为', 'C', '华为备份 App，需先探测备份格式'),
    'honor': ('荣耀', 'C', '荣耀备份 App，需先探测备份格式'),
    'oppo': ('OPPO', 'D', 'adb backup/restore（Android 12+ 常被阉割），否则需 root'),
    'oneplus': ('一加', 'D', 'adb backup/restore，否则需 root'),
    'realme': ('realme', 'D', 'adb backup/restore，否则需 root'),
    'vivo': ('vivo / iQOO', 'D', 'adb backup/restore，否则需 root'),
    'samsung': ('三星', 'D', 'adb backup/restore（One UI 多已禁用），否则需 root'),
    'google': ('Google Pixel', 'D', '原生 adb backup（Android 12+ 已禁用），建议 root'),
}


def sh(adb, serial, cmd, timeout=30):
    base = [adb] + (['-s', serial] if serial else []) + ['shell', cmd]
    try:
        r = subprocess.run(base, capture_output=True, text=True, timeout=timeout,
                           errors='replace')
        return r.stdout.strip()
    except Exception:
        return ''


def getprop(adb, serial, key):
    return sh(adb, serial, f'getprop {key}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--adb', default='adb')
    ap.add_argument('--serial', default=None)
    args = ap.parse_args()
    adb, serial = args.adb, args.serial

    print('=' * 62)
    print('S2 · 设备探测')
    print('=' * 62)

    brand_raw = getprop(adb, serial, 'ro.product.brand') or getprop(adb, serial, 'ro.product.manufacturer')
    model = getprop(adb, serial, 'ro.product.model')
    device = getprop(adb, serial, 'ro.product.device')
    android = getprop(adb, serial, 'ro.build.version.release')
    sdk = getprop(adb, serial, 'ro.build.version.sdk')
    build = getprop(adb, serial, 'ro.build.display.id')
    miui = getprop(adb, serial, 'ro.miui.ui.version.name')
    hyper = getprop(adb, serial, 'ro.mi.os.version.name')

    if not brand_raw:
        print('\n无法读取设备属性。请确认设备已连接且已授权（adb devices 状态为 device）。')
        return 1

    print(f'\n[设备]')
    print(f'  品牌      : {brand_raw}')
    print(f'  型号      : {model}  ({device})')
    print(f'  Android   : {android}  (API {sdk})')
    print(f'  系统版本  : {build}')
    if miui:
        print(f'  MIUI      : {miui}')
    if hyper:
        print(f'  HyperOS   : {hyper}')

    # 微信版本
    dump = sh(adb, serial, 'dumpsys package com.tencent.mm | grep -E "versionName|versionCode" | head -4')
    wx_ver = ''
    m = re.search(r'versionName=(\S+)', dump)
    if m:
        wx_ver = m.group(1)
    wx_code = ''
    m = re.search(r'versionCode=(\d+)', dump)
    if m:
        wx_code = m.group(1)
    print(f'\n[微信]')
    if wx_ver:
        print(f'  版本      : {wx_ver}  (versionCode={wx_code})')
        print(f'  提示      : MIUI 备份头部第 4 行应填 versionCode = {wx_code or "102"}')
    else:
        print('  未检测到 com.tencent.mm，确认微信已安装')

    # 微信分身 / 多开
    clones = sh(adb, serial, 'pm list packages | grep tencent.mm')
    if clones and len(clones.splitlines()) > 1:
        print(f'  ⚠ 检测到多个微信包（分身/多开）:')
        for ln in clones.splitlines():
            print(f'      {ln.strip()}')

    # 存储
    df = sh(adb, serial, 'df /sdcard')
    print(f'\n[存储]')
    for ln in df.splitlines():
        print(f'  {ln}')

    # root
    su = sh(adb, serial, 'which su')
    rooted = bool(su.strip())
    print(f'\n[Root]    {"已 root（' + su.strip() + '）" if rooted else "未 root"}')

    # 方案建议
    key = None
    low = brand_raw.lower()
    for k in BRAND_MAP:
        if k in low:
            key = k
            break

    print(f'\n[回传方案建议]')
    if rooted:
        print('  >> 方案 A：设备已 root，直接 adb push 回私有目录 + chown/chmod/restorecon')
        print('     这是最简单可靠的路径，可跳过备份包重建（SKILL.md S8）')
        print('     详见 references/02-brand-playbooks.md 方案 A')
    elif key:
        name, plan, desc = BRAND_MAP[key]
        print(f'  >> 方案 {plan}：{name} → {desc}')
        print(f'     详见 references/02-brand-playbooks.md 方案 {plan}')
        if plan != 'B':
            print('     ⚠ 非 MIUI 路径成功率较低，务必同时向用户说明「方案 E：只导出不回写」')
    else:
        print(f'  >> 未识别品牌 "{brand_raw}"，先探测备份目录格式：')
        print("     adb shell \"find /sdcard -maxdepth 4 -iname 'descript.xml' -o -maxdepth 4 -iname '*.bak' 2>/dev/null\"")
        print('     详见 references/02-brand-playbooks.md')

    # 备份目录探测
    print(f'\n[备份目录探测]')
    for path in ('/sdcard/MIUI/backup/AllBackup/', '/sdcard/backup/AllBackup/',
                 '/sdcard/Huawei/Backup/', '/sdcard/backup/'):
        out = sh(adb, serial, f'ls -la {path} 2>/dev/null')
        if out and 'No such' not in out:
            print(f'  {path}')
            for ln in out.splitlines()[:8]:
                print(f'    {ln}')

    print('\n' + '=' * 62)
    print('下一步：python diagnose_wechat.py  → 定位 corrupted 库并估算空间')
    print('=' * 62)
    return 0


if __name__ == '__main__':
    sys.exit(main())
