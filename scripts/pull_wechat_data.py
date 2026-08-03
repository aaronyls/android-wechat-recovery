#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
S4 一次性安全拉取（P1 电脑端闭环 + P4 exec-out 铁律）。

用 adb exec-out 把 corrupted/ 下的数据库与辅助文件拉到电脑，
并逐个比对大小确保传输完整。可选一并拉取 MIUI 备份 .bak。

用法:
    # 拉数据库及辅助文件
    python pull_wechat_data.py --hash <32位hash> --out <工作目录>

    # 额外拉 MIUI 备份包（作为回传载体）
    python pull_wechat_data.py --hash <hash> --out <workdir> \
        --bak "/sdcard/MIUI/backup/AllBackup/20260801_140743/微信(com.tencent.mm).bak"
"""
import argparse
import os
import re
import subprocess
import sys
import time

MM_EXT = '/sdcard/Android/data/com.tencent.mm/MicroMsg'
CHUNK = 4 * 1024 * 1024


def human(n):
    return f'{n / 1024 ** 3:.2f} GB' if n >= 1024 ** 3 else f'{n / 1024 ** 2:.1f} MB'


def sh(adb, serial, cmd, timeout=60):
    base = [adb] + (['-s', serial] if serial else []) + ['shell', cmd]
    try:
        return subprocess.run(base, capture_output=True, text=True,
                              timeout=timeout, errors='replace').stdout.strip()
    except Exception:
        return ''


def remote_size(adb, serial, path):
    out = sh(adb, serial, f'stat -c %s "{path}" 2>/dev/null')
    return int(out) if re.fullmatch(r'\d+', out.strip() or 'x') else 0


def pull_exec_out(adb, serial, remote, local, expect=0):
    """用 exec-out 流式拉取并显示进度。返回本地文件大小。"""
    base = [adb] + (['-s', serial] if serial else []) + ['exec-out', f'cat "{remote}"']
    print(f'  拉取 {remote}')
    if expect:
        eta = expect / (35 * 1024 ** 2)     # 经验值 ≈35MB/s
        print(f'       预期 {human(expect)}，预计耗时 {eta/60:.1f} 分钟')
    start = time.time()
    got = 0
    next_mark = 1024 ** 3
    proc = subprocess.Popen(base, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=CHUNK)
    with open(local, 'wb') as f:
        while True:
            buf = proc.stdout.read(CHUNK)
            if not buf:
                break
            f.write(buf)
            got += len(buf)
            if got >= next_mark:      # P7 每 1GB 报一次进度
                el = time.time() - start
                pct = f'{got/expect*100:.0f}%' if expect else '--'
                print(f'       {human(got)} ({pct})  {got/el/1024**2:.1f} MB/s  {el:.0f}s')
                next_mark += 1024 ** 3
    proc.stdout.close()
    proc.wait()
    el = time.time() - start
    print(f'       完成 {human(got)}，耗时 {el:.0f}s ({got/max(el,1)/1024**2:.1f} MB/s)')
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--adb', default='adb')
    ap.add_argument('--serial', default=None)
    ap.add_argument('--hash', required=True, help='微信账号 32 位 hash 目录名')
    ap.add_argument('--out', required=True, help='本地工作目录')
    ap.add_argument('--bak', default=None, help='可选：MIUI 备份 .bak 的手机端路径')
    args = ap.parse_args()

    adb, serial, h = args.adb, args.serial, args.hash
    os.makedirs(args.out, exist_ok=True)

    print('=' * 62)
    print('S4 · 一次性拉取（exec-out + 大小校验）')
    print('=' * 62)

    base_dir = f'{MM_EXT}/{h}'
    targets = [
        ('EnMicroMsg.db',     'EnMicroMsg_corrupted.db',     True),
        ('EnMicroMsg.db.sm',  'EnMicroMsg_corrupted.db.sm',  True),
        ('EnMicroMsg.db.li',  'EnMicroMsg_corrupted.db.li',  False),
        ('EnMicroMsg.db.ini', 'EnMicroMsg_corrupted.db.ini', False),
    ]

    failures = []
    print('\n[1] 拉取 corrupted/ 下的数据库与辅助文件')
    for name, local_name, required in targets:
        remote = f'{base_dir}/corrupted/{name}'
        rsize = remote_size(adb, serial, remote)
        if rsize == 0:
            # 回退到主目录（有些辅助文件未随库一起搬走）
            alt = f'{base_dir}/{name}'
            rsize = remote_size(adb, serial, alt)
            if rsize == 0:
                msg = f'  ✗ 找不到 {name}（corrupted/ 与主目录均无）'
                print(msg)
                if required:
                    failures.append(name)
                continue
            print(f'  ! {name} 不在 corrupted/，改从主目录取')
            remote = alt

        local = os.path.join(args.out, local_name)
        got = pull_exec_out(adb, serial, remote, local, rsize)

        if got == rsize:
            print(f'       ✓ 大小校验通过 ({rsize} bytes)')
        else:
            print(f'       ✗ 大小不符！手机 {rsize} vs 本地 {got} —— 需重拉')
            failures.append(name)

    # 数据库 salt 预览
    dbp = os.path.join(args.out, 'EnMicroMsg_corrupted.db')
    smp = os.path.join(args.out, 'EnMicroMsg_corrupted.db.sm')
    if os.path.exists(dbp):
        with open(dbp, 'rb') as f:
            db_salt = f.read(16)
        print(f'\n[2] salt 一致性预检')
        print(f'  数据库 salt      : {db_salt.hex()}')
        if os.path.exists(smp):
            with open(smp, 'rb') as f:
                sm = f.read(28)
            print(f'  .sm 头部 (0:12)  : {sm[:12].hex()}')
            print(f'  .sm salt (12:28) : {sm[12:28].hex()}')
            print(f'  一致性           : {"✓ 匹配" if sm[12:28] == db_salt else "✗ 不匹配（注意后续需同步）"}')

    # .ini 内容
    inip = os.path.join(args.out, 'EnMicroMsg_corrupted.db.ini')
    if os.path.exists(inip):
        try:
            txt = open(inip, 'r', encoding='utf-8', errors='replace').read()
            m = re.search(r'createmd5=([0-9a-fA-F]+)', txt)
            if m:
                print(f'  .ini createmd5   : {m.group(1)}  （密钥候选之一，见 03-key-and-repair.md）')
        except Exception:
            pass

    # 可选：拉备份包
    if args.bak:
        print(f'\n[3] 拉取 MIUI 备份包（回传载体）')
        rsize = remote_size(adb, serial, args.bak)
        if rsize == 0:
            print(f'  ✗ 找不到 {args.bak}')
            failures.append('bak')
        else:
            local = os.path.join(args.out, 'wechat_orig.bak')
            got = pull_exec_out(adb, serial, args.bak, local, rsize)
            print(f'       {"✓ 大小校验通过" if got == rsize else f"✗ 大小不符 {rsize} vs {got}"}')
            if got != rsize:
                failures.append('bak')
            else:
                with open(local, 'rb') as f:
                    head = f.read(32)
                if head.startswith(b'MIUI BACKUP'):
                    print('       格式：完整 .bak（66 字节 MIUI 头 + GNU tar）')
                else:
                    print(f'       ⚠ 未见 MIUI 头，前 16 字节: {head[:16].hex()}')
                    print('         若偏移 257 处是 ustar，则为纯 tar，打包时需自行补写 66 字节头')

    print('\n' + '=' * 62)
    if failures:
        print(f'✗ 有 {len(failures)} 项未通过：{", ".join(failures)}')
        print('  请重试。切记使用 exec-out，不要用 adb shell cat（PTY 会损坏二进制）。')
        return 1
    print('✓ 全部拉取完成且校验通过。')
    print('  【P1 提醒】后续所有处理都基于这些本地文件，不要再从手机重复读取。')
    print('  下一步 S5：python get_wechat_key.py --db <corrupted_db> --auto')
    print('=' * 62)
    return 0


if __name__ == '__main__':
    sys.exit(main())
