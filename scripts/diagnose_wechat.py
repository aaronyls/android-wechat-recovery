#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
S3 损坏诊断与空间预判。

定位微信 hash 目录、检查 corrupted/ 是否存在损坏库、统计数据量，
按 S = 2.2*T + 1.1*R 估算本地空间需求并给出工作盘调度建议。

用法:
    python diagnose_wechat.py [--adb <adb路径>] [--serial <序列号>] [--workdir <目录>]
"""
import argparse
import os
import re
import shutil
import string
import subprocess
import sys

MM_EXT = '/sdcard/Android/data/com.tencent.mm/MicroMsg'


def sh(adb, serial, cmd, timeout=120):
    base = [adb] + (['-s', serial] if serial else []) + ['shell', cmd]
    try:
        r = subprocess.run(base, capture_output=True, text=True, timeout=timeout, errors='replace')
        return r.stdout.strip()
    except Exception as e:
        return f'__ERR__ {e}'


def human(n):
    return f'{n / 1024 ** 3:.2f} GB' if n >= 1024 ** 3 else f'{n / 1024 ** 2:.1f} MB'


def size_of(adb, serial, path):
    out = sh(adb, serial, f'stat -c %s "{path}" 2>/dev/null')
    m = re.search(r'^\d+$', out.strip())
    return int(out.strip()) if m else 0


def list_drives():
    res = []
    if os.name == 'nt':
        for L in string.ascii_uppercase:
            root = f'{L}:\\'
            if os.path.exists(root):
                try:
                    t, u, f = shutil.disk_usage(root)
                    res.append((root, f))
                except OSError:
                    pass
    else:
        for root in ('/', os.path.expanduser('~')):
            if os.path.exists(root):
                t, u, f = shutil.disk_usage(root)
                res.append((root, f))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--adb', default='adb')
    ap.add_argument('--serial', default=None)
    ap.add_argument('--workdir', default=os.getcwd())
    args = ap.parse_args()
    adb, serial = args.adb, args.serial

    print('=' * 62)
    print('S3 · 损坏诊断与空间预判')
    print('=' * 62)

    # --- a) 定位 hash 目录 ---
    print('\n[1] 定位微信数据目录')
    listing = sh(adb, serial, f'ls {MM_EXT}/ 2>/dev/null')
    if listing.startswith('__ERR__') or not listing:
        print(f'  无法访问 {MM_EXT}')
        print('  可能原因：微信未安装 / Android 11+ 访问限制 / 未授权')
        print('  排查见 references/05-troubleshooting.md Q3')
        return 1

    hashes = [x.strip() for x in listing.split() if re.fullmatch(r'[a-f0-9]{32}', x.strip())]
    if not hashes:
        print(f'  未找到 32 位 hash 目录。目录内容：\n    {listing[:400]}')
        return 1

    print(f'  找到 {len(hashes)} 个账号目录：')
    infos = []
    for h in hashes:
        du = sh(adb, serial, f'du -sm {MM_EXT}/{h}/ 2>/dev/null')
        mb = 0
        m = re.match(r'(\d+)', du)
        if m:
            mb = int(m.group(1))
        cur_db = size_of(adb, serial, f'{MM_EXT}/{h}/EnMicroMsg.db')
        cor_db = size_of(adb, serial, f'{MM_EXT}/{h}/corrupted/EnMicroMsg.db')
        infos.append((h, mb, cur_db, cor_db))
        flag = '  <== 存在损坏库' if cor_db > 0 else ''
        print(f'    {h}  总计 {mb/1024:.2f} GB | 当前库 {human(cur_db)} | corrupted 库 {human(cor_db)}{flag}')

    corrupted = [i for i in infos if i[3] > 0]

    # --- b) 判定 ---
    print('\n[2] 损坏类型判定')
    if not corrupted:
        print('  ✗ 未发现 corrupted/EnMicroMsg.db')
        print('  → 不是本技能的典型场景。请按 references/05-troubleshooting.md Q1 排查：')
        print('    微信分身 / 多账号 / 数据在内部存储 / 真实删除')
        return 2

    if len(corrupted) > 1:
        print(f'  ⚠ 有 {len(corrupted)} 个账号目录都存在损坏库，需询问用户恢复哪个账号')

    target = max(corrupted, key=lambda x: x[3])
    h, total_mb, cur_db, cor_db = target
    print(f'  ✓ 确诊：微信已将损坏库移入 corrupted/，数据仍在。')
    print(f'    目标 hash : {h}')
    print(f'    损坏库大小: {human(cor_db)}   (当前空库 {human(cur_db)})')

    # corrupted 目录内容
    print('\n[3] corrupted/ 目录内容')
    out = sh(adb, serial, f'ls -la {MM_EXT}/{h}/corrupted/ 2>/dev/null')
    for ln in out.splitlines():
        print(f'    {ln}')

    aux = {}
    for ext in ('.sm', '.li', '.ini'):
        s = size_of(adb, serial, f'{MM_EXT}/{h}/corrupted/EnMicroMsg.db{ext}')
        aux[ext] = s
        if s == 0:
            print(f'    ⚠ 缺少 EnMicroMsg.db{ext}，稍后需从主目录取')

    # --- c) 空间估算 ---
    print('\n[4] 空间需求估算')
    T = total_mb * 1024 ** 2          # 备份包大小 ≈ 微信数据总量
    R = cor_db                        # 待修复库
    need = 2.2 * T + 1.1 * R
    print(f'    T (微信数据总量/备份包) = {human(T)}')
    print(f'    R (待修复数据库)        = {human(R)}')
    print(f'    S = 2.2xT + 1.1xR       = {human(need)}   <-- 本地最低空闲需求')

    workdir = os.path.abspath(args.workdir)
    try:
        _, _, free = shutil.disk_usage(workdir)
    except OSError:
        free = 0
    print(f'\n    工作目录: {workdir}')
    print(f'    可用空间: {human(free)}')

    if free >= need:
        print(f'    >> 空间充足，可继续 S4。')
    else:
        print(f'    >> ✗ 空间不足，缺少 {human(need - free)}')
        cands = [(r, f) for r, f in list_drives()
                 if f >= need and not workdir.upper().startswith(r.upper())]
        cands.sort(key=lambda x: -x[1])
        if cands:
            print('    >> 建议询问用户是否切换到以下磁盘：')
            for i, (r, f) in enumerate(cands):
                print(f'         {r}  可用 {human(f)}' + ('   <-- 推荐' if i == 0 else ''))
            print('\n    【交互门 G2】用 AskUserQuestion 询问用户：')
            print(f'      "当前工作盘剩余 {human(free)}，本次至少需要 {human(need)}。')
            print(f'       检测到 {cands[0][0]} 剩余 {human(cands[0][1])}，是否切换到该盘处理？"')
        else:
            print('    >> 所有磁盘均不足，需用户先清理空间或外接存储。')

    # 手机空间
    print('\n[5] 手机空间检查（回传需要 ≈T 空闲）')
    df = sh(adb, serial, 'df /sdcard')
    for ln in df.splitlines():
        print(f'    {ln}')
    print(f'    回传 .bak 需手机额外 ≈ {human(T)} 空闲；')
    print(f'    若在手机上新建备份，还需再 ≈ {human(T)}（合计 ≈ {human(2*T)}）。')
    print('    空间紧张时：跳过"手机端新建备份"，直接拉现成备份在电脑改完推回。')

    # 汇总
    print('\n' + '=' * 62)
    print('诊断结论')
    print('=' * 62)
    print(f'  场景确认  : EnMicroMsg.db 内部损坏 → 已被移入 corrupted/')
    print(f'  目标 hash : {h}')
    print(f'  待修复库  : {human(R)}')
    print(f'  空间需求  : {human(need)}  (当前工作盘可用 {human(free)})')
    print(f'\n  下一步 S4：')
    print(f'    python pull_wechat_data.py --hash {h} --out <workdir>')
    return 0


if __name__ == '__main__':
    sys.exit(main())
