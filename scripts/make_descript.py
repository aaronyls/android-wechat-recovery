#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
S8-b 计算并生成 descript.xml（内置断言）。

四个尺寸字段必须分别计算，算错的典型症状是：
「恢复进度到 98% 后突然跳 100% 并提示备份文件损坏」。

    bakFileSize        = .bak 文件实际字节数
    pkgSize            = tar 内所有文件数据之和（不含 512 头、不含对齐填充、不含 L 条目）
    size               = bakFileSize + 65536
    transingTotalSize  = pkgSize
    completedSize      = pkgSize

用法:
    # 有原 descript.xml 作模板（推荐，保留其余字段）
    python make_descript.py --bak wechat_final.bak --template descript_orig.xml --out descript.xml

    # 仅计算不出文件
    python make_descript.py --bak wechat_final.bak --calc-only
"""
import argparse
import os
import re
import sys
import time

MIUI_HEADER_SIZE = 66


def calc_pkg_size(bak_path):
    """遍历 tar 条目累加文件数据字节数"""
    pkg = 0
    entries = 0
    with open(bak_path, 'rb') as f:
        head = f.read(16)
        f.seek(MIUI_HEADER_SIZE if head.startswith(b'MIUI BACKUP') else 0)
        while True:
            hdr = f.read(512)
            if len(hdr) < 512 or hdr == b'\0' * 512:
                break
            s = hdr[124:136].rstrip(b'\x00 ').decode('ascii', errors='replace')
            try:
                size = int(s, 8) if s else 0
            except ValueError:
                size = 0
            tf = chr(hdr[156]) if hdr[156] else '0'
            padded = size + (512 - size % 512) if size % 512 else size
            if tf == 'L':          # GNU long name 数据不计入 pkgSize
                f.seek(padded, 1)
                continue
            pkg += size
            entries += 1
            f.seek(padded, 1)
    return pkg, entries


def set_field(xml, key, value):
    """替换 XML 中 key="..." 或 <key>...</key> 形式的值"""
    new, n = re.subn(rf'({re.escape(key)}\s*=\s*")[^"]*(")', rf'\g<1>{value}\g<2>', xml)
    if n:
        return new, n
    new, n = re.subn(rf'(<{re.escape(key)}>)[^<]*(</{re.escape(key)}>)', rf'\g<1>{value}\g<2>', xml)
    return new, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bak', required=True)
    ap.add_argument('--template', default=None, help='原始 descript.xml，作为模板')
    ap.add_argument('--out', default=None)
    ap.add_argument('--calc-only', action='store_true')
    ap.add_argument('--date-ms', type=int, default=None, help='13 位毫秒时间戳，默认取当前时间')
    args = ap.parse_args()

    print('=' * 62)
    print('S8-b · descript.xml 计算与生成')
    print('=' * 62)

    if not os.path.exists(args.bak):
        print(f'[错误] 备份文件不存在: {args.bak}')
        return 1

    bak_file_size = os.path.getsize(args.bak)
    print(f'\n扫描 {args.bak} ...')
    t0 = time.time()
    pkg_size, entries = calc_pkg_size(args.bak)
    size_val = bak_file_size + 65536

    print(f'  扫描条目数        : {entries:,}   （耗时 {time.time()-t0:.0f}s）')
    print(f'\n[计算结果]')
    print(f'  bakFileSize       = {bak_file_size:,}   ({bak_file_size/1024**3:.2f} GB)')
    print(f'  pkgSize           = {pkg_size:,}   ({pkg_size/1024**3:.2f} GB)')
    print(f'  size              = {size_val:,}   (= bakFileSize + 65536)')
    print(f'  transingTotalSize = {pkg_size:,}')
    print(f'  completedSize     = {pkg_size:,}')

    # 断言
    print(f'\n[断言校验]')
    checks = [
        ('bakFileSize == 文件实际字节', bak_file_size == os.path.getsize(args.bak)),
        ('pkgSize > 0', pkg_size > 0),
        ('pkgSize < bakFileSize', pkg_size < bak_file_size),
        ('size == bakFileSize + 65536', size_val == bak_file_size + 65536),
        ('pkgSize/bakFileSize 在合理区间 (0.90~1.00)', 0.90 <= pkg_size / bak_file_size < 1.0),
    ]
    all_ok = True
    for name, ok in checks:
        print(f'  [{"PASS" if ok else "FAIL"}] {name}')
        all_ok &= ok
    if not all_ok:
        print('\n  ✗ 断言未通过，不生成 descript.xml。请检查 .bak 是否完整。')
        return 1

    if args.calc_only or not args.out:
        print('\n（--calc-only 模式，未生成文件）')
        print('  把上述值填入 descript.xml 的对应字段即可。')
        return 0

    if not args.template or not os.path.exists(args.template):
        print(f'\n[错误] 需要 --template 指定原始 descript.xml 作为模板。')
        print('  从手机拉取：adb exec-out "cat /sdcard/MIUI/backup/AllBackup/<日期>/descript.xml" > descript_orig.xml')
        return 1

    xml = open(args.template, 'r', encoding='utf-8', errors='replace').read()
    date_ms = args.date_ms if args.date_ms else int(time.time() * 1000)

    updates = {
        'bakFileSize': bak_file_size,
        'pkgSize': pkg_size,
        'size': size_val,
        'transingTotalSize': pkg_size,
        'completedSize': pkg_size,
        'date': date_ms,
    }
    print(f'\n[写入字段]')
    for k, v in updates.items():
        xml, n = set_field(xml, k, v)
        print(f'  {k:<18} = {v}   {"✓" if n else "! 模板中未找到该字段"}')

    with open(args.out, 'w', encoding='utf-8') as f:
        f.write(xml)

    # 回读验证
    back = open(args.out, 'r', encoding='utf-8', errors='replace').read()
    verified = all(str(v) in back for v in updates.values())
    print(f'\n输出: {args.out}  ({os.path.getsize(args.out)} bytes)')
    print(f'回读校验: {"✓ 通过" if verified else "✗ 有字段未写入，请手工检查"}')

    print(f'\n  提示：备份目录名 yyyyMMdd_HHmmss 应与 date={date_ms} 对应，')
    print(f'        即 {time.strftime("%Y%m%d_%H%M%S", time.localtime(date_ms/1000))}')
    print(f'\n  下一步 S9：push .bak 与 descript.xml 到手机备份目录')
    return 0 if verified else 1


if __name__ == '__main__':
    sys.exit(main())
