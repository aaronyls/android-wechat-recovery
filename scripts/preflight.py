#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
S0 环境与磁盘预检。

检查 ADB / Python / sqlcipher3 / 各磁盘空间，并在给定数据量时估算需求、
给出工作盘调度建议。

用法:
    python preflight.py [--adb <adb路径>] [--tar-size-gb 32] [--db-size-gb 3.1] [--workdir <目录>]

空间需求模型:  S = 2.2 * T + 1.1 * R
    T = 原始备份/tar 大小   R = 待修复数据库大小
"""
import argparse
import os
import shutil
import string
import subprocess
import sys


def human(n):
    return f"{n / 1024 ** 3:.1f} GB"


def find_adb(explicit=None):
    """定位 adb 可执行文件"""
    if explicit and os.path.isfile(explicit):
        return explicit
    found = shutil.which('adb') or shutil.which('adb.exe')
    if found:
        return found
    for cand in [
        r'C:\platform-tools\adb.exe',
        r'C:\Android\platform-tools\adb.exe',
        os.path.expanduser('~/platform-tools/adb.exe'),
        os.path.expanduser('~/Library/Android/sdk/platform-tools/adb'),
        '/usr/local/bin/adb',
        '/usr/bin/adb',
    ]:
        if os.path.isfile(cand):
            return cand
    return None


def check_adb(adb):
    if not adb:
        return False, 'ADB 未找到。请下载 Google platform-tools 并解压，或用 --adb 指定路径。'
    try:
        ver = subprocess.run([adb, 'version'], capture_output=True, text=True, timeout=15)
        line = ver.stdout.strip().splitlines()[0] if ver.stdout.strip() else 'unknown'
    except Exception as e:
        return False, f'ADB 无法执行: {e}'

    try:
        out = subprocess.run([adb, 'devices'], capture_output=True, text=True, timeout=30).stdout
    except Exception as e:
        return False, f'adb devices 失败: {e}'

    devices, unauthorized, offline = [], [], []
    for ln in out.splitlines()[1:]:
        parts = ln.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        {'device': devices, 'unauthorized': unauthorized, 'offline': offline}.get(state, []).append(serial)

    if devices:
        return True, f'{line} | 已连接设备: {", ".join(devices)}'
    if unauthorized:
        return False, f'{line} | 设备未授权，请在手机上确认「允许 USB 调试」并勾选「一律允许」'
    if offline:
        return False, f'{line} | 设备 offline，执行 adb kill-server 后重新连接'
    return False, f'{line} | 未检测到设备，检查数据线 / USB 调试开关 / 驱动'


def check_sqlcipher():
    for mod in ('sqlcipher3', 'pysqlcipher3'):
        try:
            __import__(mod)
            return True, f'{mod} 可用'
        except ImportError:
            continue
    v = sys.version_info
    if v < (3, 12):
        hint = 'pip install sqlcipher3-binary'
    else:
        hint = 'pip install sqlcipher3  （Python 3.12+ 无 binary 轮子，需编译；或 pip install pysqlcipher3）'
    return False, f'未安装 SQLCipher 绑定。Python {v.major}.{v.minor} 建议: {hint}'


def list_drives():
    """返回 [(盘符/挂载点, total, used, free)]"""
    result = []
    if os.name == 'nt':
        for letter in string.ascii_uppercase:
            root = f'{letter}:\\'
            if not os.path.exists(root):
                continue
            try:
                t, u, f = shutil.disk_usage(root)
                result.append((root, t, u, f))
            except OSError:
                pass
    else:
        for root in ('/', os.path.expanduser('~'), '/Volumes', '/mnt'):
            if os.path.exists(root):
                try:
                    t, u, f = shutil.disk_usage(root)
                    result.append((root, t, u, f))
                except OSError:
                    pass
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--adb')
    ap.add_argument('--tar-size-gb', type=float, default=None, help='原始备份/tar 大小(GB)')
    ap.add_argument('--db-size-gb', type=float, default=None, help='待修复数据库大小(GB)')
    ap.add_argument('--workdir', default=os.getcwd())
    args = ap.parse_args()

    print('=' * 62)
    print('S0 · 环境与磁盘预检')
    print('=' * 62)

    ok_all = True

    # Python
    v = sys.version_info
    print(f'\n[Python]  {v.major}.{v.minor}.{v.micro}  ({sys.executable})')

    # SQLCipher
    ok, msg = check_sqlcipher()
    print(f'[SQLCipher] {"OK  " if ok else "FAIL"} {msg}')
    ok_all &= ok

    # ADB
    adb = find_adb(args.adb)
    ok, msg = check_adb(adb)
    print(f'[ADB]     {"OK  " if ok else "FAIL"} {msg}')
    if adb:
        print(f'          路径: {adb}')
    ok_all &= ok

    # 磁盘
    print('\n[磁盘空间]')
    drives = list_drives()
    for root, t, u, f in drives:
        print(f'  {root:<12} 总 {human(t):>9}  已用 {human(u):>9}  可用 {human(f):>9}')

    # 空间需求
    if args.tar_size_gb is not None and args.db_size_gb is not None:
        need = (2.2 * args.tar_size_gb + 1.1 * args.db_size_gb) * 1024 ** 3
        print(f'\n[空间需求]  S = 2.2 x {args.tar_size_gb}GB + 1.1 x {args.db_size_gb}GB = {human(need)}')

        workdir = os.path.abspath(args.workdir)
        try:
            _, _, free = shutil.disk_usage(workdir)
        except OSError:
            free = 0
        print(f'  工作目录: {workdir}  可用 {human(free)}')

        if free >= need:
            print(f'  >> 空间充足，可直接开工。')
        else:
            ok_all = False
            print(f'  >> 空间不足！缺少 {human(need - free)}')
            candidates = [(r, f) for r, _, _, f in drives if f >= need and not workdir.upper().startswith(r.upper())]
            candidates.sort(key=lambda x: -x[1])
            if candidates:
                print('  >> 以下磁盘满足需求，建议询问用户是否切换工作盘：')
                for r, f in candidates:
                    print(f'       {r}  可用 {human(f)}   <-- 推荐' if r == candidates[0][0]
                          else f'       {r}  可用 {human(f)}')
            else:
                print('  >> 所有磁盘均不满足，需先由用户清理空间或外接存储。')
    else:
        print('\n[空间需求]  未提供数据量，跳过估算。')
        print('            拿到备份大小 T 与数据库大小 R 后，重跑：')
        print('            python preflight.py --tar-size-gb T --db-size-gb R --workdir <工作目录>')

    print('\n' + '=' * 62)
    print('预检结果: ' + ('全部通过，可以进入 S1' if ok_all else '存在未通过项，请先处理'))
    print('=' * 62)
    return 0 if ok_all else 1


if __name__ == '__main__':
    sys.exit(main())
